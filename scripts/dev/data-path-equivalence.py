#!/usr/bin/env python3
"""Does the NEW address equal the OLD one? Measured, per call site, on a real overlay.

HOS-5 phase 6 converts engine call sites off the migration seam
(`get_outputs_dir`, `moved_home`, and their siblings in
`scripts/utils/workspace.py`) and onto the one front door,
`scripts/utils/data_path.data_path`. Every such conversion makes a claim that
nothing in the tree checks: **the two expressions name the same directory.**

Until 2026-09-21 a wrong conversion went red anyway, because 29 254 checks stood
over it. The HOS-54 split left 115. So the claim now has to be MEASURED rather
than reviewed, which is what this tool is: it evaluates the old expression and
the new one against the same live data root and compares the absolute paths.

## Three outcomes, and the third is not a success

    agreed       both expressions evaluated and named the SAME absolute path
    disagreed    both evaluated and named DIFFERENT paths. The conversion is wrong.
    not-static   at least one expression could not be evaluated from the source
                 alone, because an argument is a variable, an f-string, or a
                 call this evaluator does not know.

`not-static` is reported in its own column and counted in its own total. Folding
it into "agreed" is the failure this tool exists to prevent: a site whose
address is assembled at run time is a site a human has to read, not one a
machine has cleared. `verify` exits non-zero when anything DISAGREED, and prints
the not-static list whether or not it exits non-zero, so the residue is visible
rather than absent.

## The precedent this generalises

The phase-3 yard proved the registry address-preserving by an identity over 48
statically evaluable `moved_home` sites:

    moved_home(legacy, family, leaf) == write_dir(family, leaf)
                                        / legacy.relative_to(data_root)

48 agreed, 0 disagreed. That identity is specific to one helper and one shape.
This tool asks the same question of ANY pair of expressions, which is what a
conversion across seven helpers needs.

## No `eval`, and the reason is not only the rule that forbids it

Both expressions are parsed with `ast` and walked by `_evaluate` below, which
accepts exactly four node kinds: a string constant, a call whose callee is a
name in `ALLOWED` and whose arguments are themselves acceptable, a `/` join, and
a parenthesised group. Everything else raises `NotStatic` by name. So the
refusal to evaluate is a CLASSIFICATION rather than a crash, which is what makes
the third outcome reportable at all; `eval` would either answer or raise, and a
`NameError` says nothing about whether the site is convertible.

## Three refusal rules, and what each one cost to learn

Every rule in `convert_file` below was written AFTER the tool had already
reported a conversion clean. None was designed in advance, and the list is
therefore a record of how many ways an equal address can be an unequal
behaviour:

    1  the value is one CANDIDATE among several, so its FAILURE was contractual
    4  the result is consumed inside a `try` whose handler swallows the failure
    5  a human wrote, in prose, that this file must not be converted

**RULES 2 AND 3 WERE REMOVED** (rule 3 on 2026-09-23 with the
executive-workspace helpers it guarded, rule 2 on 2026-09-25 when the creating
helper it guarded was deleted), and the numbers were deliberately not closed
up: each is named in the prose of other files, and renumbering would repoint
those sentences at a rule they were not written about.

Rules 4 and 5 were IMPLEMENTED on 2026-09-21 in the second slice, and the first
of them was implemented because the tool re-committed the defect. MEASURED that
day on a clean tree at `21846fb2`, which already carried the first slice's
revert: `convert --only scripts/ --only .claude/` converted exactly one site,
`scripts/context-floor-audit.py:280`, which is the site the first slice found by
hand and reverted. A refusal recorded only in a report is a refusal the next run
does not make.

Rule 4 is the shape that site has. `scripts/context-floor-audit.py` reads its
result inside `try: ... except Exception: memory = 0`, so a front door that
raises turns a real byte count into a silent zero and the row reads healthy.
`_failure_is_swallowed` below refuses any site sitting in the BODY of a `try`
whose handler catches something a `DataPathError` satisfies and does not
re-raise.

Rule 5 is the limit that matters, and it is only half closed. **This tool reads
code and cannot read prose.** `.claude/hooks/memory-reconcile.py` carried a
comment saying in as many words that this exact conversion had been tried in
phase 3, measured, reverted, and must not be repeated until `names:` has a
shipped default the way `services:` does. The tool converted it anyway, and the
only thing that caught it was a person reading the paragraph. So the prohibition
now has a form the tool CAN read, `DO_NOT_CONVERT` below, written on the line
above the prose that argues it. That closes the case where somebody remembers to
write the marker. It does not close the case where they write only the
paragraph, and nothing in this tree does. Read the site before trusting the
verdict.

A marker written INSIDE a function refuses that function alone; one at module
level still refuses the whole file. The constant's own note carries why, and the
short of it is that four of the five files carrying such a paragraph on
2026-09-22 also hold seam sites that are ordinary work, so a file-wide marker
would have cost more than the prohibition is worth and would therefore not have
been written at all.

Asking must not write. `get_runtime_dir` used to be evaluated here with
`create=False` forced for that reason; it was deleted under HOS-5 § 6 on
2026-09-25, so an expression calling it no longer evaluates at all, and a
`data_write_dir` expression is answered as `data_path`, which creates nothing.

Usage:
  # every seam call site, with the registry name that already names its address
  python scripts/dev/data-path-equivalence.py plan --out plan.tsv

  # check a conversion: columns file, line, old_expr, new_expr
  python scripts/dev/data-path-equivalence.py verify --pairs plan.tsv
  python scripts/dev/data-path-equivalence.py verify --pairs plan.tsv --floor 40
"""
from __future__ import annotations

import argparse
import ast
import csv
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.utils import workspace as _workspace  # noqa: E402
from scripts.utils import paths as _paths  # noqa: E402
from scripts.utils.colors import GREEN, YELLOW, RED, CYAN, GRAY, BOLD, RESET  # noqa: E402
from scripts.utils.data_path import (  # noqa: E402
    DataPathError, data_path, known_names,
)
from scripts.utils.repo_files import git_index_paths, IndexUnreadable  # noqa: E402
from scripts.utils.ast_closure import callee as _callee  # noqa: E402

#: The seam. A call site is any call to one of these names, and the expression
#: under test is that call plus every `/` join above it.
#:
#: Derived from nothing: this IS the list the conversion is against, and it is
#: the same seven names the briefs and the guards use. It is not read out of
#: `workspace.py` because membership here is a statement about what is being
#: RETIRED, and a module's namespace cannot say that.
SEAM_HELPERS = (
    "get_data_root", "get_outputs_dir", "get_runtime_dir", "get_plans_dir",
    "moved_home", "moved_file", "moved_homes",
)

#: The front door. A converted site calls one of these. `data_write_dir`, the
#: one that creates, joined on 2026-09-25.
FRONT_DOOR = ("data_path", "data_read", "data_read_required", "data_write_dir", "data_write_file")

# `WORKSPACE_KIND_ROOTS` and the rule-3 closure over the executive-workspace
# helpers stood here until 2026-09-23; the helpers themselves were deleted on
# 2026-09-25 (HOS-130, HOS-132). An equality this tool measures is measured on
# the tree it runs against, and always was.

#: Every helper an expression under test may CALL, resolved to the real callable
#: so the tool measures the tree's own behaviour rather than a model of it.
#:
#: Wider than `SEAM_HELPERS` on purpose: a seam call takes another helper's
#: answer as its argument (`moved_home(get_data_root() / "threads", ...)`),
#: so refusing the inner one would report the outer as `not-static` and hide a
#: site that is perfectly evaluable.
_ROOT_HELPERS = (
    "get_workspace_root", "get_data_root", "get_outputs_dir",
    "get_plans_dir", "get_knowledge_dir",
    "get_auto_memory_dir", "get_templates_dir",
    "get_data_config_dir", "get_reference_dir",
    "moved_home", "moved_file", "moved_homes", "moved_dir",
)


class NotStatic(RuntimeError):
    """The expression cannot be evaluated from the source alone.

    Carries the node kind that stopped it, so the report says WHY a site needs a
    human rather than only that it does.
    """


def _build_allowed() -> dict[str, Callable]:
    """The callable table, looked up by name in the modules that define them."""
    table: dict[str, Callable] = {}
    for name in _ROOT_HELPERS:
        fn = getattr(_workspace, name, None) or getattr(_paths, name, None)
        if fn is not None:
            table[name] = fn
    table["Path"] = Path
    table["data_path"] = data_path
    # The write operation answers the same address and then mkdirs. This tool
    # runs over the operator's live overlay to answer a question, and a question
    # must not leave a directory behind, so it is answered as `data_path`.
    table["data_write_dir"] = data_path
    table["data_write_file"] = data_path
    return table


ALLOWED = _build_allowed()

#: Every helper that ANSWERS a data address. An import of one of these that this
#: conversion left unread is this conversion's to remove; anything else in the
#: same statement is not. See `_prune_and_add_imports`.
_ADDRESS_HELPERS = frozenset(SEAM_HELPERS) | frozenset(_ROOT_HELPERS)


# ============================================================
# Evaluating one expression
# ============================================================

def _evaluate(node: ast.AST) -> Any:
    """The value of `node`, or `NotStatic` naming what stopped it.

    Four node kinds and no more. Each refusal names the kind, so the plan can
    say `not-static: Name` rather than `not-static`, which is the difference
    between a residue somebody can work and a residue somebody re-derives.
    """
    if isinstance(node, ast.Constant):
        # `bool` is accepted, and it is the one constant kind that had to be
        # argued for. It is never a path segment; it is `legacy=True`, the
        # keyword eleven seam helpers carry. Refusing it reported 17 sites as
        # `not-static` on 2026-09-21 whose addresses are perfectly evaluable,
        # which is a residue manufactured by the tool rather than found in the
        # tree. `_evaluate` is not a type checker: the helper it is handed to
        # decides what the value means.
        if isinstance(node.value, (str, int, bool)):
            return node.value
        raise NotStatic(f"a {type(node.value).__name__} constant")
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return Path(_evaluate(node.left)) / str(_evaluate(node.right))
    if isinstance(node, ast.Call):
        name = _callee(node)
        fn = ALLOWED.get(name)
        if fn is None:
            raise NotStatic(f"a call to {name or 'an expression'}()")
        args = [_evaluate(a) for a in node.args]
        kwargs = {}
        for kw in node.keywords:
            if kw.arg is None:
                raise NotStatic("a **kwargs splat")
            kwargs[kw.arg] = _evaluate(kw.value)
        return fn(*args, **kwargs)
    if isinstance(node, ast.Attribute):
        raise NotStatic(f"an attribute .{node.attr}")
    if isinstance(node, ast.Name):
        raise NotStatic(f"the variable {node.id!r}")
    if isinstance(node, ast.JoinedStr):
        raise NotStatic("an f-string")
    raise NotStatic(f"a {type(node).__name__} node")


class Evaluated(NamedTuple):
    path: Path | None
    why: str          # "" when it evaluated, else the reason it did not

    @property
    def static(self) -> bool:
        return self.path is not None


def evaluate_expression(text: str) -> Evaluated:
    """One expression's absolute path, or the reason there is none.

    A `DataPathError` is NOT `not-static`: the expression was evaluable and the
    resolver REFUSED it, which is a real answer about a real name, so it travels
    as its own reason and the caller can tell the two apart in the report.
    """
    try:
        tree = ast.parse(text.strip(), mode="eval")
    except SyntaxError as exc:
        return Evaluated(None, f"unparseable ({exc.msg})")
    try:
        value = _evaluate(tree.body)
    except NotStatic as exc:
        return Evaluated(None, f"not static: {exc}")
    except DataPathError as exc:
        return Evaluated(None, f"refused: {type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 - a helper's own failure is an answer
        return Evaluated(None, f"raised: {type(exc).__name__}: {exc}")
    if isinstance(value, tuple):
        # `data_read` and `moved_homes` answer with several addresses. One of
        # them is not the answer, and picking one would be the guess this tool
        # exists to refuse.
        return Evaluated(None, f"several addresses ({len(value)})")
    if not isinstance(value, (str, Path)):
        return Evaluated(None, f"a {type(value).__name__}, not a path")
    return Evaluated(Path(value), "")


# ============================================================
# Finding the call sites
# ============================================================

class Site(NamedTuple):
    file: str
    line: int
    expr: str
    helper: str


def _outermost_join(tree: ast.AST) -> dict[int, ast.AST]:
    """For every node, its parent, so a call can climb to the top of its `/` chain."""
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    return parents


def sites_in(text: str, rel: str, callees: Iterable[str]) -> list[Site]:
    """Every call to one of `callees`, widened to the whole `/` join above it.

    Widened because the address is the JOIN, not the call: converting
    `get_outputs_dir() / "operations" / "council"` and checking only
    `get_outputs_dir()` would compare two roots and call a wrong tail proved.
    A nested call (the `moved_home` argument) is skipped, because the outer call
    already carries it and reporting both counts one address twice.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    wanted = set(callees)
    parents = _outermost_join(tree)
    found: list[Site] = []
    seen: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _callee(node) not in wanted:
            continue
        top: ast.AST = node
        while True:
            parent = parents.get(id(top))
            if isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Div) \
                    and parent.left is top:
                top = parent
                continue
            break
        # A call sitting inside another allowed call is that call's argument.
        enclosing = parents.get(id(top))
        if isinstance(enclosing, ast.Call) and _callee(enclosing) in ALLOWED:
            continue
        if id(top) in seen:
            continue
        seen.add(id(top))
        found.append(Site(rel, node.lineno, ast.unparse(top), _callee(node)))
    return sorted(found, key=lambda s: (s.file, s.line))


def engine_python(engine_root: Path) -> list[str]:
    """Tracked `.py` files, excluding the seam's own definition and this tool."""
    skip = {"scripts/utils/workspace.py", "scripts/utils/paths.py",
            "scripts/dev/data-path-equivalence.py"}
    return [p for p in git_index_paths(engine_root)
            if p.endswith(".py") and p not in skip]


# ============================================================
# The registry, read backwards
# ============================================================

def address_index(data_root: Path | None = None) -> dict[str, list[str]]:
    """`{absolute path: [every name that resolves to it]}`, for the parameterless names.

    A parametrised name (`crm-contact person=...`) has no single address and is
    absent here by construction rather than by omission; `plan` reports the
    count of names it could not place so the gap is a number rather than a
    silence.
    """
    index: dict[str, list[str]] = {}
    for name in known_names(data_root):
        try:
            # The marker sits on the CALL's own line, which is where the guard
            # reports it. The name here is not an address somebody spelled: it
            # comes from `known_names()`, so this loop asks the registry what it
            # holds, one name at a time. That is the same shape the CLI's own
            # `data_path(args.name, ...)` carries a marker for, and the same
            # reason: a file-level exemption would stop checking this module
            # entirely, which is the trade `SEAM_ALLOWLIST` refused on
            # 2026-09-19.
            resolved = data_path(name, data_root=data_root)  # leak-guard: ok (enumerating known_names(), not an address spelled here)
        except (DataPathError, OSError):
            continue
        index.setdefault(str(resolved), []).append(name)
    return index


# ============================================================
# Rewriting a file
# ============================================================

#: The import a converted file needs, and the one it may shed.
FRONT_DOOR_IMPORT = "from scripts.utils.data_path import data_path"

#: The module half of it, for placing the line in an alphabetical import block.
FRONT_DOOR_MODULE_NAME = "scripts.utils.data_path"

#: The one prohibition a human can write that this tool obeys.
#:
#: A file carrying this string anywhere in its text is skipped WHOLE, and the
#: line carrying it is printed as the reason, so the refusal quotes the author
#: rather than paraphrasing them.
#:
#: It exists because of `.claude/hooks/memory-reconcile.py`, which on 2026-09-21
#: carried a paragraph saying that this exact conversion had been tried in phase
#: 3, measured against three red tests, and reverted the same day, and which
#: this tool converted anyway. Prose is invisible to an AST walker. A marker is
#: not: it is the smallest thing a person can write that a machine can refuse
#: on, and it sits on the line ABOVE the paragraph rather than replacing it,
#: because the machine needs the string and the next reader needs the reason.
#:
#: What it does NOT close, stated here rather than left to be found: a file
#: whose author wrote only the paragraph is converted exactly as before. Nothing
#: in this tree detects that, the checks that would have are parked, and this
#: constant does not change it.
#:
#: SCOPE, since 2026-09-22, and the reason is what happened when the remaining
#: seam sites were read one at a time. Five of the seven carry a dated paragraph
#: arguing, in the file's own words, that the site must stay on the seam, and
#: four of those five sit in files holding OTHER seam sites that are ordinary
#: work. A marker that refuses the whole file would have bought the prohibition
#: by making every future site in `scripts/workspace-health.py` invisible, and a
#: refusal nobody can afford to write is a refusal nobody writes. So a marker
#: inside a function refuses THAT FUNCTION, and a marker at module level still
#: refuses the whole file, which is the only shape this tree carried before
#: (`.claude/hooks/memory-reconcile.py`, whose marker sits inside the one
#: function holding its one seam site, so MEASURED 2026-09-22 nothing about that
#: file's answer changes). Narrower is never weaker here: every site the marker
#: no longer covers is judged by the five rules exactly as an unmarked site is.
#:
#: Before writing a new function marker, check `plan --out` for the OTHER seam
#: sites inside that function: a function marker silences its neighbours too.
DO_NOT_CONVERT = "data-path-equivalence: do-not-convert"


def _forbidden_scopes(text: str, tree: ast.AST) -> tuple[list[str], list[dict]]:
    """Where each `do-not-convert` marker refuses: one function, or the file.

    Returns `(whole-file refusals, per-function refusals)`. The first is
    non-empty when a marker sits outside every `def`, which is the module-level
    shape and the one this tool carried before 2026-09-22.

    The INNERMOST enclosing function wins, so a marker written inside a nested
    helper does not silence its parent. A marker on a decorator line, or on the
    blank line above a `def`, is outside the function by `ast`'s own reckoning
    and therefore refuses the file: that is the conservative direction, and it
    is the one to fail toward when a person's intent is ambiguous.
    """
    functions = [n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    whole_file: list[str] = []
    scoped: list[dict] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if DO_NOT_CONVERT not in line:
            continue
        holding = [f for f in functions
                   if f.lineno <= number <= (f.end_lineno or f.lineno)]
        if not holding:
            whole_file.append(f"line {number}: {line.strip()}")
            continue
        inner = min(holding,
                    key=lambda f: (f.end_lineno or f.lineno) - f.lineno)
        scoped.append({"name": inner.name, "start": inner.lineno,
                       "end": inner.end_lineno or inner.lineno,
                       "line": number, "text": line.strip()})
    return whole_file, scoped


def _is_a_candidate(node: ast.AST, parents: dict[int, ast.AST],
                    compared: set[str]) -> bool:
    """True when this value is one option among several rather than THE answer.

    Two shapes, and both were found the hard way on 2026-09-21 by a prose sweep
    that went looking for stale sentences and found two live regressions.

    A TUPLE or LIST display: `scripts/firecrawl.py` iterates
    `(data_path(...), get_reference_dir() / "search-domains.md")` and returns
    the first that exists. The tuple is built EAGERLY, so a front door that
    raises takes the engine fallback down with it, and a public clone stops
    warning "no domains are being blocked" and starts raising instead.

    A COMPARISON: `scripts/marp_render.py` asks the records first and reads
    "the answer equals the legacy path" as the signal to fall through to the
    write seam. `moved_home` ANSWERS `legacy` when nothing resolves; the front
    door RAISES, so the comparison is never reached and the fall-through is
    unreachable by that route.

    Both sites pass the differential check, because the address really is the
    same on this overlay. What the check cannot see is that the old call's
    FAILURE was part of its contract. Address equality is not behavioural
    equality, and this function is the only place in the tool that knows it.

    A comparison reaches the value through a NAME more often than directly, so
    the assignment case is checked too. `marp_render` writes
    `decks = moved_home(...)` and compares `decks` on the next line, and a rule
    that only looked at the call's own parent node let it through.
    """
    parent = parents.get(id(node))
    if isinstance(parent, (ast.Tuple, ast.List, ast.Set, ast.Compare)):
        return True
    if isinstance(parent, ast.Assign):
        return any(isinstance(t, ast.Name) and t.id in compared
                   for t in parent.targets)
    return False


# Rule 2 (refuse a creating `get_runtime_dir` call) was removed 2026-09-25 when
# `data_write_dir` took over creation at 0700; rule numbers are not shifted.

#: Every exception name a `DataPathError` would satisfy on its way out.
#:
#: `DataPathError` subclasses `RuntimeError`, so these four names and a bare
#: `except:` are the handlers that CATCH the front door's refusal. `OSError`,
#: `KeyError` and their kind are deliberately absent: a handler that catches one
#: of those does not catch this, so the site is not swallowed and refusing it
#: would be this tool manufacturing a residue.
_CATCHES_A_REFUSAL = frozenset({
    "BaseException", "Exception", "RuntimeError", "DataPathError",
    "UnknownDataPath",
})

def _handler_would_swallow(handler: ast.ExceptHandler) -> bool:
    """True when this handler catches a front-door refusal and does not re-raise."""
    kinds = handler.type
    if kinds is None:
        caught = True                       # a bare `except:` catches everything
    else:
        parts = kinds.elts if isinstance(kinds, ast.Tuple) else [kinds]
        names = {p.id if isinstance(p, ast.Name) else
                 p.attr if isinstance(p, ast.Attribute) else "" for p in parts}
        caught = bool(names & _CATCHES_A_REFUSAL)
    if not caught:
        return False
    return not any(isinstance(n, ast.Raise) for n in ast.walk(handler))


def _failure_is_swallowed(node: ast.AST, parents: dict[int, ast.AST]) -> bool:
    """True when converting this site would CHANGE what a handler does with a failure.

    The fourth refusal shape, and the one that cost the most to learn. The seam
    helpers ANSWER whatever they are given; the front door RAISES on a name the
    registry does not carry. Where the caller already wraps the read in a
    `try` whose handler assigns a default, the two are equal as addresses, equal
    in the happy path, and opposite in the only case anybody wrote the handler
    for: the address that cannot be resolved. A real number becomes a silent
    zero, and the row the operator reads says healthy.

    MEASURED 2026-09-21 on `scripts/context-floor-audit.py:280`, which reads
    `try: memory = _file_bytes(get_data_root() / "auto-memory" / "MEMORY.md")`
    under `except Exception: memory = 0`. The first slice converted it, caught
    it by hand three passes later, and reverted it; on a clean tree carrying
    that revert, this tool converted it again, which is what made the rule
    mechanical rather than a note.

    Only the `try` BODY counts. A site in a handler, an `else` or a `finally`
    is not the thing being guarded, and folding those in would refuse sites
    whose failure nothing catches.

    From 2026-09-22 to 2026-09-25 the rule also ran in the MIRROR direction for
    one helper: `get_runtime_dir` refused with `RuntimeDirError`, a
    **ValueError**, where the front door raises `DataPathError`, a
    **RuntimeError**, so a handler written for the old refusal would let the new
    one escape (`scripts/utils/dead_letter.py:214`, whose `record()` promises
    "Never raises"). That helper was deleted under HOS-5 § 6 on 2026-09-25 and
    its sites were converted by hand with each handler read, so the mirror half
    went with it: every other seam helper ANSWERS rather than refuses, and for
    those the two directions are one question.
    """
    child = node
    parent = parents.get(id(child))
    while parent is not None:
        if isinstance(parent, ast.Try) and any(stmt is child for stmt in parent.body):
            for handler in parent.handlers:
                if _handler_would_swallow(handler):
                    return True
        child, parent = parent, parents.get(id(parent))
    return False


def _compared_names(tree: ast.AST) -> set[str]:
    """Every name this module reads inside a comparison."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for part in [node.left, *node.comparators]:
                out.update(n.id for n in ast.walk(part) if isinstance(n, ast.Name))
    return out


class Rewrite(NamedTuple):
    site: Site
    name: str
    old_path: str
    #: The source text left UNTOUCHED to the right of the replaced expression.
    #: Empty when the whole site was replaced. See `_named_prefix`.
    tail: str = ""


def _segment(node: ast.AST) -> tuple[int, int, int, int]:
    return (node.lineno - 1, node.col_offset, node.end_lineno - 1, node.end_col_offset)


def _top_nodes(tree: ast.AST, callees: Iterable[str]) -> list[tuple[ast.AST, ast.Call]]:
    """The same widening `sites_in` does, returning the NODES so a span is available."""
    wanted = set(callees)
    parents = _outermost_join(tree)
    tops: list[tuple[ast.AST, ast.Call]] = []
    seen: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _callee(node) not in wanted:
            continue
        top: ast.AST = node
        while True:
            parent = parents.get(id(top))
            if isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Div) \
                    and parent.left is top:
                top = parent
                continue
            break
        enclosing = parents.get(id(top))
        if isinstance(enclosing, ast.Call) and _callee(enclosing) in ALLOWED:
            continue
        if id(top) not in seen:
            seen.add(id(top))
            tops.append((top, node))
    return tops


def _replace_spans(text: str, spans: list[tuple[tuple[int, int, int, int], str]]) -> str:
    """Swap each source span for its replacement, LAST first.

    Last first because every earlier span's offsets are the ones `ast` measured
    on the original text, and a replacement made above them moves the ones
    below. Rewriting by span and not by `ast.unparse` of the whole module is the
    point: unparsing a file reformats every line in it, which turns a 6-line
    conversion into a 900-line diff nobody can review.
    """
    lines = text.splitlines(keepends=True)
    for (l0, c0, l1, c1), new in sorted(spans, key=lambda s: s[0], reverse=True):
        head = lines[l0][:c0]
        tail = lines[l1][c1:]
        lines[l0:l1 + 1] = [head + new + tail]
    return "".join(lines)


def _used_names(tree: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}


def _prune_and_add_imports(text: str, used_before: set[str]) -> str:
    """Add the front door's import where it is now used; drop a seam name nobody calls.

    Both halves, because either alone leaves the file wrong: without the first
    it does not run, and without the second it carries an import of the thing it
    was converted off, which reads to the next person as a site that was missed.

    **Only a data-address helper this conversion actually ORPHANED is pruned**,
    and both halves of that are load-bearing. `used_before` is the set of names
    the file read BEFORE the rewrite, so a helper that was already dead stays
    where it is: MEASURED 2026-09-21, `scripts/export-antigravity-config.py`
    imports `get_workspace_root` and never called it, at `HEAD` and after, so
    removing it would be cleaning up somebody else's mess.

    The restriction to the helper family was
    bought with a real regression rather than reasoned out. The first version
    dropped every imported name the AST could not see used, and on
    `scripts/census-bench.py` it deleted `PINNED_KEYS`, which carries
    `# noqa: F401 - re-exported so tests pin the pin set from one place`: an
    import that is unused BY DESIGN. Only the names this conversion orphaned are
    this conversion's to remove, which is `.claude/rules/development-standards.md`
    under Restraint, and it is also the only rule that makes the pruning safe
    without reading every caller of every module.
    """
    tree = ast.parse(text)
    used = _used_names(tree)
    lines = text.splitlines(keepends=True)
    edits: list[tuple[tuple[int, int, int, int], str]] = []
    #: Where the seam's own import sat, and at what indentation. The front
    #: door's import goes THERE and not at module level, because a seam import
    #: is not always a module-level one. MEASURED 2026-09-21 on
    #: `scripts/utils/checkpoint_paths.py`, which imports its siblings inside a
    #: `try:` in a function body on purpose: a module-level insertion put
    #: `from scripts.utils.data_path import data_path` at column 0 between
    #: `try:` and its block and the file stopped parsing. The suite stayed green
    #: because nothing under `tests/` imports that module, which is the exact
    #: shape this phase has to assume from now on.
    displaced: tuple[int, int] | None = None

    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if not node.module.startswith("scripts."):
            continue
        keep = [a for a in node.names
                if a.asname or a.name in used
                or a.name not in _ADDRESS_HELPERS or a.name not in used_before]
        if len(keep) == len(node.names):
            continue
        if displaced is None:
            displaced = (node.lineno - 1, node.col_offset)
        if not keep:
            # The whole statement goes, and its line with it, so the span
            # reaches the start of the next line rather than the last
            # character, which would leave a blank line where an import was.
            edits.append(((node.lineno - 1, 0, node.end_lineno, 0), ""))
            continue
        raw = "".join(lines[node.lineno - 1:node.end_lineno])
        tail = raw[raw.rfind(")") + 1:] if ")" in raw else raw.split("import", 1)[1]
        comment = tail[tail.index("#"):].rstrip("\n") if "#" in tail else ""
        one = " " * node.col_offset + f"from {node.module} import "
        one += ", ".join(a.name for a in keep)
        if comment:
            one += "  " + comment
        edits.append(((node.lineno - 1, 0, node.end_lineno - 1,
                       len(lines[node.end_lineno - 1].rstrip("\n"))), one))

    text = _replace_spans(text, edits) if edits else text

    if "data_path" not in _used_names(ast.parse(text)) or FRONT_DOOR_IMPORT in text:
        return text

    lines = text.splitlines(keepends=True)
    if displaced is not None:
        at, indent = displaced
        suffix = ""
    else:
        # Nothing was displaced, so the file reached the seam some other way and
        # the import belongs at module level. Alphabetical where that block
        # already is; after the last one where it is not. Nothing in this
        # repository enforces import order (ruff selects no isort rule), so this
        # is about the diff a person reads, not about a gate.
        parsed = ast.parse(text)
        tops = [n for n in parsed.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        if not tops:
            raise NotStatic("no import at all to anchor the front door's")
        at, indent = tops[-1].end_lineno, 0
        for node in tops:
            if (getattr(node, "module", None) or "") > FRONT_DOOR_MODULE_NAME:
                at = node.lineno - 1
                break
        suffix = "  # noqa: E402" if "noqa: E402" in lines[tops[-1].end_lineno - 1] else ""
    lines.insert(at, " " * indent + FRONT_DOOR_IMPORT + suffix + "\n")
    return "".join(lines)


def _division_chain(node: ast.AST) -> list[ast.AST]:
    """`a / b / c`, then `a / b`, then `a`. The whole site first, shortest last."""
    chain: list[ast.AST] = []
    cur = node
    while True:
        chain.append(cur)
        if isinstance(cur, ast.BinOp) and isinstance(cur.op, ast.Div):
            cur = cur.left
            continue
        return chain


def _named_prefix(node: ast.AST, index: dict[str, list[str]],
                  picks: dict[str, str]) -> tuple[ast.AST, str, str, str] | None:
    """The longest part of `node` the registry names. Returns (node, name, path, tail).

    Until 2026-09-21 this tool only ever asked about the WHOLE site, because the
    address is the join and not the call. That is right about what to COMPARE
    and wrong about what to REPLACE: the registry names directories, and a site
    is usually a named directory with a literal or a variable hung off it, so
    asking only the whole expression reported a site as `unnamed` when the only
    unnamed part of it was a filename.

    MEASURED 2026-09-21 over the 148 sites the plan called `unnamed` or
    `not-static`: 20 of them have a named prefix, 15 from `unnamed` and 5 from
    `not-static`. The five are the ones that matter most, because a variable
    tail can never be evaluated and those sites were otherwise permanently a
    human's.

    The tail is returned rather than rebuilt so the caller can put it in the
    pairs file. The source bytes of the tail are NOT touched by the rewrite,
    which is what makes the prefix comparison sufficient: equal prefixes joined
    to identical text are equal paths. Stated, and not left implied, because
    `verify` then compares a prefix while reporting a site, and a reader is
    owed the difference.
    """
    whole = ast.unparse(node)
    for candidate in _division_chain(node):
        got = evaluate_expression(ast.unparse(candidate))
        if not got.static:
            continue
        old = str(got.path)
        names = index.get(old, [])
        if old in picks:
            name = picks[old]
        elif len(names) == 1:
            name = names[0]
        else:
            # Ambiguous or unknown at THIS length. A shorter prefix may still
            # resolve, so keep peeling rather than stopping: the alternative
            # reports "4 names resolve to <the data root>" for every site in
            # the tree, which is a refusal nobody can act on.
            continue
        return candidate, name, old, whole[len(ast.unparse(candidate)):]
    return None


def convert_file(engine_root: Path, rel: str, index: dict[str, list[str]],
                 picks: dict[str, str]) -> tuple[list[Rewrite], list[str]]:
    """Convert every unambiguously named seam site in one file. Returns (done, skipped)."""
    path = engine_root / rel
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    whole_file, forbidden = _forbidden_scopes(text, tree)
    if whole_file:
        return [], [f"{rel}: the file forbids this conversion in its own words "
                    f"at {whole_file[0]}, and this tool obeys that rather than "
                    f"judging it"]

    done: list[Rewrite] = []
    skipped: list[str] = []
    spans: list[tuple[tuple[int, int, int, int], str]] = []
    parents = _outermost_join(tree)
    compared = _compared_names(tree)
    for node, call in _top_nodes(tree, SEAM_HELPERS):
        expr = ast.unparse(node)
        marker = next((m for m in forbidden
                       if m["start"] <= node.lineno <= m["end"]), None)
        if marker:
            skipped.append(
                f"{rel}:{node.lineno}: `{marker['name']}()` forbids this "
                f"conversion in its own words at line {marker['line']}, and "
                f"this tool obeys that rather than judging it: "
                f"{marker['text']}")
            continue
        if _is_a_candidate(node, parents, compared):
            skipped.append(
                f"{rel}:{node.lineno}: the old value is ONE CANDIDATE among "
                f"several, so its failure mode is part of the contract and an "
                f"equal address does not prove an equal behaviour: {expr}")
            continue
        # The name is resolved here rather than after the remaining rules
        # because rule 3 used to have a question to ask about it. It no longer
        # exists and the order is kept anyway: `_named_prefix` only reads, so a
        # site refused by any rule below is refused for the same reason and in
        # the same words it was before 2026-09-23.
        hit = _named_prefix(node, index, picks)
        if _failure_is_swallowed(node, parents):
            skipped.append(
                f"{rel}:{node.lineno}: the result is read inside a `try` whose "
                f"handler turns a failure into a default, so converting it "
                f"would move the refusal across that handler in one direction "
                f"or the other: {expr}")
            continue
        if hit is None:
            continue
        target, name, old, tail = hit
        spans.append((_segment(target), f'data_path({name!r})'))
        done.append(Rewrite(Site(rel, node.lineno, expr, _callee(call)),
                            name, old, tail))

    if spans:
        path.write_text(
            _prune_and_add_imports(_replace_spans(text, spans), _used_names(tree)),
            encoding="utf-8")
    return done, skipped


# ============================================================
# Subcommands
# ============================================================

#: The `root` column was dropped 2026-09-23 with rule 3, which is the only
#: thing that ever filled it: it named the workspace-kind helper an expression
#: resolved through, and no rule asks that any more.
PLAN_HEADER = ("file", "line", "helper", "old_expr", "old_path",
               "candidate_names", "verdict", "why")

#: Columns read by NAME rather than by unpacking from both ends. It used to
#: unpack, and a column appended at the end would have shifted `verdict` and
#: `why` by one with nothing going red: the tally would have counted the reason
#: string as a verdict and printed four zeroes.
_VERDICT_COLUMN = PLAN_HEADER.index("verdict")
_HELPER_COLUMN = PLAN_HEADER.index("helper")


def cmd_plan(args: argparse.Namespace) -> int:
    engine_root = args.engine_root
    files = engine_python(engine_root)
    index = address_index(args.data_root)

    root = _paths.get_data_root().resolve() if args.data_root is None \
        else args.data_root.resolve()

    rows: list[list[str]] = []
    tally = {"named": 0, "unnamed": 0, "root-only": 0, "not-static": 0}
    for rel in files:
        try:
            text = (engine_root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for site in sites_in(text, rel, SEAM_HELPERS):
            got = evaluate_expression(site.expr)
            if not got.static:
                verdict, names, old = "not-static", "", ""
            else:
                old = str(got.path)
                names = ",".join(index.get(old, []))
                # The data ROOT is not a resource address and has no logical
                # name by design: HOS-5 § 3 keeps exactly one mechanism that
                # LOCATES the repositories, and the registry says where things
                # are INSIDE the root it hands back. A bare `get_data_root()`
                # is that mechanism being used correctly. Counting it as
                # `unnamed` would read as 84 addresses owed a registry entry,
                # and the first person to close that gap would put the root
                # itself in the file the root is needed to find.
                if Path(old).resolve() == root:
                    verdict = "root-only"
                elif names:
                    verdict = "named"
                else:
                    verdict = "unnamed"
            tally[verdict] += 1
            rows.append([site.file, str(site.line), site.helper, site.expr,
                         old, names, verdict, got.why])

    if not rows:
        print(f"{RED}REFUSED{RESET}: no seam call site found in "
              f"{len(files)} tracked Python file(s). A plan over an empty "
              f"corpus is not a plan.", file=sys.stderr)
        return 2

    if args.out:
        with args.out.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh, delimiter="\t", lineterminator="\n")
            writer.writerow(PLAN_HEADER)
            writer.writerows(rows)
        print(f"{GREEN}wrote{RESET} {args.out}", file=sys.stderr)

    total = len(rows)
    print(f"{BOLD}Helper call sites{RESET}  {total} in "
          f"{len({r[0] for r in rows})} file(s), over {len(files)} tracked .py")
    print(f"  {GREEN}named{RESET}       {tally['named']:4d}  a registry name "
          f"already resolves to that exact address")
    print(f"  {YELLOW}unnamed{RESET}     {tally['unnamed']:4d}  evaluated, and no "
          f"registry name resolves there yet")
    print(f"  {GRAY}root-only{RESET}   {tally['root-only']:4d}  the data root "
          f"itself, which the registry does not name and must not")
    print(f"  {CYAN}not-static{RESET}  {tally['not-static']:4d}  the address is "
          f"assembled at run time; a human reads these")
    _print_by_helper(rows)
    print(f"{GRAY}registry: {len(index)} distinct address(es) over "
          f"{sum(len(v) for v in index.values())} name(s){RESET}")
    return 0


#: The seven names of `SEAM_HELPERS`, split by what HOS-5 asks of each. The
#: headline above totals all seven, which is why it is a CENSUS and not a debt:
#: MEASURED 2026-09-22 on `main`, 245 sites over the seven, of which 75 are the
#: fallback the task says to delete. Reading the 245 as work owed put the end of
#: this phase 170 sites further away than it is.
#:
#: The split is read out of the issue text rather than judged from the code,
#: because the code cannot say what is being retired and a comment that guesses
#: is the shape this tool exists to catch.
#:
#: § 6, translated: "remove the live seam, the fallback to old addresses,
#: the migration redirects and the wrappers no longer needed." Three of the seven ARE
#: that fallback in the literal sense: each takes an address as the pre-migration
#: tree spelled it and answers with the address the tree has now. When no
#: pre-migration address is left, they answer nothing.
FALLBACK_HELPERS = ("moved_home", "moved_file", "moved_homes")

#: § 3, translated: "The engine must have one small mechanism for finding the
#: repository roots and loading the registry." That mechanism is this one name, and § 3
#: requires it to exist, so its 89 sites are not debt under any reading. The
#: registry says where things are INSIDE the root; it cannot name the root.
ROOT_MECHANISM = ("get_data_root",)

#: The remainder. Each answers a data address without going through the
#: registry, which § 4's "there must be no second implementation of the logic"
#: argues against, and each is a candidate for § 6's "wrappers no longer needed". (translated) That
#: was an open decision until the operator's word of 2026-09-25, "old way should
#: go out and do not stay in the workspace", which puts them in § 6. The
#: objection that had held it open was `get_runtime_dir` creating the store root
#: at 0700, which `data_path` does not do and must not; it was answered the same
#: day by the front door's write operation `data_write_dir`, and that helper was
#: deleted. They are still printed apart, as the wrappers § 6 retires.
WRAPPER_HELPERS = tuple(h for h in SEAM_HELPERS
                        if h not in FALLBACK_HELPERS + ROOT_MECHANISM)


def _print_by_helper(rows: list[list[str]]) -> None:
    """The same sites again, split by which helper is called.

    The four verdicts above answer "can this site be converted mechanically?".
    This answers "is this site the work?", and the two are independent: a
    `not-static` call to `moved_file` is debt a human has to read, while a
    `not-static` call to `get_data_root` is the root being composed at run time
    and is nobody's debt at all. Printing one without the other is what let a
    census read as a backlog.
    """
    per: dict[str, dict[str, int]] = {}
    for row in rows:
        helper, verdict = row[_HELPER_COLUMN], row[_VERDICT_COLUMN]
        per.setdefault(helper, {}).setdefault(verdict, 0)
        per[helper][verdict] += 1

    order = ("named", "unnamed", "root-only", "not-static")
    groups = ((FALLBACK_HELPERS, RED, "the fallback section 6 deletes"),
              (WRAPPER_HELPERS, YELLOW, "wrappers section 6 retires "
                                        "(operator, 2026-09-25)"),
              (ROOT_MECHANISM, GRAY, "the root mechanism section 3 requires"))

    print(f"\n{BOLD}By helper{RESET}  "
          f"{GRAY}{'  '.join(f'{v:>10}' for v in order)}       total{RESET}")
    for group, colour, label in groups:
        names = [h for h in group if h in per]
        if not names:
            continue
        subtotal = sum(sum(per[h].values()) for h in names)
        print(f"  {colour}{label}{RESET} {GRAY}({subtotal}){RESET}")
        for helper in sorted(names, key=lambda h: -sum(per[h].values())):
            cells = "  ".join(f"{per[helper].get(v, 0):>10}" for v in order)
            print(f"    {helper:<20}{cells}  {BOLD}{sum(per[helper].values()):>6}"
                  f"{RESET}")

    debt = sum(sum(per[h].values()) for h in FALLBACK_HELPERS if h in per)
    print(f"\n{BOLD}Section 6 debt{RESET}  {debt}  "
          f"{GRAY}calls to {', '.join(FALLBACK_HELPERS)}. This is the number "
          f"that reaches zero.{RESET}")

    # No rule-3 count is printed: rule 3 is gone (2026-09-23), and `convert`
    # names the rule that refused each site, one line per site.

def cmd_convert(args: argparse.Namespace) -> int:
    """Rewrite the unambiguous sites in a SLICE, and write the pairs to verify.

    A slice and not the tree, because HOS-5 phase 6 is ordered by blast radius:
    leaf scripts, then the modules other code imports, then `.claude/hooks/**`
    last, since a hook loads on every tool call and a broken edit disarms the
    session's own walls. The tool takes the slice as an argument rather than
    knowing the order, so the order stays the operator's and is visible in the
    command that was run.
    """
    engine_root = args.engine_root
    index = address_index(args.data_root)
    picks = dict(p.split("=", 1) for p in args.pick)

    files = [f for f in engine_python(engine_root)
             if any(f.startswith(p) for p in args.only)
             and not any(f.startswith(p) for p in args.skip)]
    if not files:
        print(f"{RED}REFUSED{RESET}: no tracked Python file matches "
              f"{args.only}.", file=sys.stderr)
        return 2

    done: list[Rewrite] = []
    skipped: list[str] = []
    for rel in files:
        try:
            d, s = convert_file(engine_root, rel, index, picks)
        except SyntaxError as exc:
            skipped.append(f"{rel}: unparseable ({exc})")
            continue
        done.extend(d)
        skipped.extend(s)

    # `old_expr` is what was REPLACED, which is the whole site when `tail` is
    # empty and its named prefix when it is not. The pair `verify` compares is
    # therefore always the pair that was actually swapped, and `tail` carries
    # the untouched remainder so the report can say which rows compared less
    # than the whole expression.
    rows = [[r.site.file, str(r.site.line),
             r.site.expr[:len(r.site.expr) - len(r.tail)],
             f'data_path({r.name!r})', r.old_path, r.tail] for r in done]
    if args.out:
        with args.out.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh, delimiter="\t", lineterminator="\n")
            writer.writerow(("file", "line", "old_expr", "new_expr",
                             "old_path", "tail"))
            writer.writerows(rows)
        print(f"{GREEN}wrote{RESET} {args.out}", file=sys.stderr)

    print(f"{BOLD}Converted{RESET} {len(done)} site(s) in "
          f"{len({r.site.file for r in done})} file(s), over "
          f"{len(files)} file(s) in the slice")
    for line in skipped:
        print(f"  {YELLOW}skipped{RESET} {line}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """The acceptance check: old and new must name the same absolute path."""
    with args.pairs.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        header = set(reader.fieldnames or ())
        rows = list(reader)
    # The HEADER answers this, never `rows[0]`. One expression was asked to
    # settle two different questions and got the second one wrong: a pairs file
    # that `convert` wrote for a slice it refused ENTIRELY carries the header
    # and no rows, `rows[0]` is never reached, and the branch below then told
    # the operator the file "has no column(s) ['new_expr', 'old_expr']" while
    # both names sat on line 1 of it. MEASURED 2026-09-22 in the third slice,
    # where all twenty `named` sites were refused by the five rules above and
    # every one of the four pairs files came back with that sentence. The
    # empty-corpus refusal further down is the one that says the true thing,
    # and a file with a correct header could never reach it.
    missing = {"old_expr", "new_expr"} - header
    if missing:
        print(f"{RED}REFUSED{RESET}: {args.pairs} has no column(s) "
              f"{sorted(missing)}.", file=sys.stderr)
        return 2

    agreed: list[dict] = []
    disagreed: list[tuple[dict, str, str]] = []
    not_static: list[tuple[dict, str]] = []
    for row in rows:
        if not (row.get("new_expr") or "").strip():
            continue
        old = evaluate_expression(row["old_expr"])
        new = evaluate_expression(row["new_expr"])
        if not old.static or not new.static:
            not_static.append((row, old.why or new.why))
        elif old.path == new.path:
            agreed.append(row)
        else:
            disagreed.append((row, str(old.path), str(new.path)))

    checked = len(agreed) + len(disagreed) + len(not_static)
    if checked == 0:
        print(f"{RED}REFUSED{RESET}: {args.pairs} carries {len(rows)} row(s) and "
              f"none of them names a new expression, so nothing was compared. A "
              f"pass over an empty corpus is not a pass.", file=sys.stderr)
        return 2

    print(f"{BOLD}Differential acceptance{RESET}  {checked} pair(s) from {args.pairs}")
    print(f"  {GREEN}agreed{RESET}      {len(agreed):4d}  old and new name the same path")
    print(f"  {RED}disagreed{RESET}   {len(disagreed):4d}  they name different paths")
    print(f"  {CYAN}not-static{RESET}  {len(not_static):4d}  one side could not be "
          f"evaluated from source")

    # A row carrying a tail compared the site's named PREFIX, because the rest
    # of it is a filename or a variable the rewrite did not touch. Counted and
    # listed separately rather than folded into `agreed`, for the same reason
    # `not-static` has its own column: a pair that measured less than the whole
    # expression has to say so, or the number reads as more coverage than the
    # method bought.
    #
    # `.get`, because `--pairs` promises only `old_expr` and `new_expr`. Until
    # 2026-09-25 a hand-written pairs file without `file` and `line` columns
    # raised `KeyError: 'file'` on its first disagreement, after the summary
    # had printed and before the disagreement itself could, which is the one
    # line the run exists to show.
    def site(row: dict) -> str:
        return f"{row.get('file') or '?'}:{row.get('line') or '?'}"

    with_tail = [r for r in agreed if (r.get("tail") or "").strip()]
    if with_tail:
        print(f"  {GRAY}of those, {len(with_tail)} compared the named PREFIX of "
              f"the site; the tail was left byte-for-byte{RESET}")
        for row in with_tail:
            print(f"  {GRAY}prefix{RESET} {site(row)}"
                  f"  tail {row['tail'].strip()}")

    for row, why in not_static:
        print(f"  {CYAN}not-static{RESET} {site(row)}  {why}")
    for row, old, new in disagreed:
        print(f"\n{RED}{BOLD}DISAGREED{RESET} {site(row)}")
        print(f"  old {row['old_expr']}\n   -> {old}")
        print(f"  new {row['new_expr']}\n   -> {new}")

    if disagreed:
        print(f"\n{RED}{BOLD}FAIL{RESET} -- {len(disagreed)} conversion(s) change "
              f"the address they resolve to.", file=sys.stderr)
        return 1
    if args.floor is not None and len(agreed) < args.floor:
        print(f"\n{RED}{BOLD}FAIL{RESET} -- {len(agreed)} agreed, below the floor "
              f"of {args.floor}. A shrinking corpus passes this check by "
              f"checking less, which is why the floor is here.", file=sys.stderr)
        return 1
    print(f"\n{GREEN}OK{RESET} -- no conversion changes its address.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Old expression versus new: do they name the same directory?")
    ap.add_argument("--engine-root", type=Path, default=None,
                    help="the checkout to read (default: the one this file sits in)")
    ap.add_argument("--data-root", type=Path, default=None,
                    help="resolve against this overlay instead of the configured one")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_plan = sub.add_parser("plan", help="every seam call site, with the name that fits it")
    p_plan.add_argument("--out", type=Path, default=None, help="write the TSV here")

    p_conv = sub.add_parser("convert", help="rewrite the unambiguous sites in a slice")
    p_conv.add_argument("--only", action="append", required=True, metavar="PREFIX",
                        help="repeatable: convert files whose path starts with this")
    p_conv.add_argument("--skip", action="append", default=[], metavar="PREFIX",
                        help="repeatable: leave this prefix for a later slice")
    p_conv.add_argument("--pick", action="append", default=[], metavar="PATH=NAME",
                        help="repeatable: resolve an address several names answer for")
    p_conv.add_argument("--out", type=Path, default=None,
                        help="write the old/new pairs here, for `verify`")

    p_ver = sub.add_parser("verify", help="check old against new, pair by pair")
    p_ver.add_argument("--pairs", type=Path, required=True,
                       help="TSV carrying at least old_expr and new_expr")
    p_ver.add_argument("--floor", type=int, default=None,
                       help="fail when fewer than this many pairs agreed")

    args = ap.parse_args(argv)
    args.engine_root = (args.engine_root.resolve() if args.engine_root
                        else Path(__file__).resolve().parent.parent.parent)

    try:
        if args.cmd == "plan":
            return cmd_plan(args)
        if args.cmd == "convert":
            return cmd_convert(args)
        return cmd_verify(args)
    except IndexUnreadable as exc:
        print(f"{RED}REFUSED{RESET}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
