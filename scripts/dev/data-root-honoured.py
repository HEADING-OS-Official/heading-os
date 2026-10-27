#!/usr/bin/env python3
"""Does a function that ACCEPTS a data root actually build its answer under it?

The shape this finds, and it is the shape HOS-75 was opened on: a function takes
a `data_root` parameter, forwards it to something deeper, and builds the BASE of
its path with a call that does not take that parameter at all. The forwarded
argument then reaches only the steps after the base, so the answer is rooted in
whatever the machine's own root helper returns. A caller that passed a scratch
root for isolation reads and writes the operator's live overlay, and nothing
raises.

    # the original, scripts/utils/calibration_store.py, before HOS-75
    def calibration_dir(*, data_root=None):
        return moved_home(get_datastore_dir(legacy=True).joinpath(*SUBDIR),
                          "build", "runbook", data_root=data_root)
                          #  ^ honoured            ^ base ignores it

## Why the AST and not a grep

A substring scan for `data_root` goes red on every fix that quotes the bad
expression to explain it, and this repository's fixes are written that way on
purpose: the paragraph above is itself three such quotations. The scan below
reads signatures and call graphs, so a docstring naming `get_datastore_dir` is
invisible to it and a wrapper three hops from a root helper is not.

## What a "base" is, mechanically

A call to a BLIND helper: one that resolves a data root, directly or through
wrappers, AND whose own signature has no root parameter, so a caller has no way
to tell it which tree to answer for. The set is DERIVED, never listed:

  1. seeds -- the root resolver in `scripts/utils/paths.py` and
     `scripts/utils/workspace.py` (four until 2026-09-25; see `_SEEDS`);
  2. closure -- every module-level function in those two files that reaches a
     seed, at any depth (`get_knowledge_dir` -> `get_data_root`);
  3. per-file closure -- the same again over the searched file's own
     module-level functions, so a local wrapper counts;
  4. minus every helper whose signature DOES take a root, because passing the
     root to those is the correct thing a fixed call site does.

Steps 1-3 use `module_functions` and `closure_over` from
`scripts/utils/ast_closure.py`, shared with `scripts/dev/import-env-purity.py`,
because a second copy of a graph walk is the defect this repository fixes most
often.

## Three classes, and only the first is the HOS-75 shape

    direct   the blind call sits INSIDE the same call expression that forwards
             the root (or reaches it through a variable bound in that function).
             The base and the honoured argument are one expression apart, so the
             parameter provably does not reach the base.
    loose    both happen in the function, but not in one expression. Read it:
             the base may be a different path entirely, and this class is the
             tool saying so rather than guessing.
    unpassed the helper CAN be told which root and the caller holding one did
             not tell it. Reported separately because step 4 above removes such
             a helper from the blind set, and the whole finding with it.

## Why `unpassed` exists, and it is a repair of this tool rather than a feature

Step 4 is right and the narrowing it causes is not. When a helper gains a root
parameter it leaves the blind set, and every caller that holds a root and still
does not pass it leaves the report with it: the count improves because the
instrument stopped looking (MEASURED 2026-09-25, HOS-115: eight of eleven sites
that left the report were unchanged). `unpassed` asks that second question.

The two are kept apart rather than merged into one count, because the repair
differs. A `loose` site needs a root parameter threaded into a helper that has
none. An `unpassed` site needs one argument added at the call.

Exit code is 1 when anything is `direct`, so this can be wired to a gate; it is
0 with a printed list when only `loose` and `unpassed` sites stand. `unpassed`
does not gate, for the same reason `loose` does not: it names a site to read,
not a proven defect.

Usage:
  python scripts/dev/data-root-honoured.py
  python scripts/dev/data-root-honoured.py --only scripts/utils --quiet
  python scripts/dev/data-root-honoured.py --self-test
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path
from typing import Iterable, NamedTuple

_ENGINE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ENGINE))

from scripts.utils.colors import GREEN, YELLOW, RED, CYAN, GRAY, BOLD, RESET  # noqa: E402
from scripts.utils.ast_closure import closure_over, module_functions  # noqa: E402

#: The resolver every data address in this tree is ultimately rooted in. Until
#: 2026-09-25 there were four: `get_exec_data_root` went with the executive
#: model that day (HOS-130), and `get_corporate_root` and `get_personal_root`,
#: which by then both answered `get_data_root()`, were deleted by HOS-132.
#: `get_workspace_root` is deliberately absent: it names the ENGINE clone, which
#: no `data_root` argument was ever meant to move.
_SEEDS = ("get_data_root",)

#: The modules that define them. Read, not listed, so a helper that grows a
#: root branch tomorrow is covered the same day with nothing edited here.
_ROOT_MODULES = ("scripts/utils/paths.py", "scripts/utils/workspace.py",
                 "scripts/utils/data_path.py")

#: A parameter in this shape is a ROOT the caller supplies.
_ROOT_PARAM_SUFFIX = "_root"

#: Root parameters that name the ENGINE clone rather than a data tree. A
#: function handed one of these and resolving a data root beside it is doing the
#: right thing: the two are different trees, and the whole engine/data split says
#: so. Skipped rather than reported, and the count is PRINTED, because an
#: exclusion nobody sees reads as coverage.
_ENGINE_ROOT_PARAMS = frozenset({"workspace_root", "engine_root", "repo_root",
                                 "project_root"})

_DEFAULT_TREES = ("scripts", ".claude", "tests")


#: Attributes that turn a path into a STRING. A blind helper read for one of
#: these is not a base: `get_outputs_dir().name` is the four letters "outputs"
#: on every tree there is, and a supplied root could not change it.
_STRING_ATTRS = frozenset({"name", "stem", "suffix", "suffixes"})


class Finding(NamedTuple):
    path: Path
    line: int
    func: str
    param: str
    base: str
    forwarded_to: str
    klass: str


class Scan(NamedTuple):
    findings: list[Finding]
    #: Engine-root parameters deliberately not judged, so a caller can print
    #: their number rather than drop them in silence.
    skipped: list[str]
    #: Files actually parsed. A floor over this is what keeps a scan that walked
    #: nothing from reading as a clean tree.
    files: int
    #: The derived blind-helper set, for the same reason.
    blind: frozenset[str]
    #: `unpassed` findings, held in their own list rather than folded into
    #: `findings`. Two reasons. The repair differs, so a single count would mix
    #: "thread a parameter into a helper" with "add one argument at a call". And
    #: `findings` is what a live test in tests/test_data_root_no_bypass.py pins
    #: against a pinned pre-fix blob, so folding a new class into it would change
    #: an assertion about a defect that has nothing to do with this class.
    unpassed: list[Finding] = []
    #: The complement of `blind` over the same reaching set. A floor over this
    #: is what keeps the new class from silently reporting zero: an empty
    #: tellable set produces no findings and looks exactly like a clean tree.
    tellable: frozenset[str] = frozenset()


def _param_names(node: ast.AST) -> list[str]:
    a = node.args
    return [p.arg for p in (list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs))]


def _accepts_a_root(node: ast.AST) -> bool:
    return any(_is_data_root_param(n) for n in _param_names(node))


def _is_root_param(name: str) -> bool:
    return name == "root" or name.endswith(_ROOT_PARAM_SUFFIX)


def _is_data_root_param(name: str) -> bool:
    return _is_root_param(name) and name not in _ENGINE_ROOT_PARAMS


def _callee(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


def blind_helpers(tree_of: dict[str, ast.AST]) -> set[str]:
    """Helpers that reach a root and cannot be told which one."""
    reaching, defs = _reaching(tree_of)
    return {name for name in reaching
            if name not in defs or not _accepts_a_root(defs[name])}


def tellable_helpers(tree_of: dict[str, ast.AST]) -> dict[str, int | None]:
    """Helpers that reach a root and CAN be told which one, with WHERE it goes.

    The exact complement of `blind_helpers` over the same reaching set, and it
    exists because the complement used to be nothing at all. A helper that gains
    a root parameter leaves the blind set, which is correct, and takes every one
    of its unpassing callers out of the report with it, which is not. The
    docstring at the top of this file carries the measurement.

    The value is the root's POSITIONAL index, or None when it is keyword-only.
    Carrying the index rather than just the name is what stops this reporting a
    call that passes the root positionally, and that is not a hypothetical: the
    first version of this function returned a bare set, the detector read only
    keyword arguments, and it reported `_overlay_routing_rules(data_root)` and
    `_relative_to_a_tree(norm, data_root)` as unpassed on the day the root
    reached both. Two false positives inside the repair that prompted this
    class. MEASURED 2026-09-25.
    """
    reaching, defs = _reaching(tree_of)
    out: dict[str, int | None] = {}
    for name in reaching:
        if name not in defs or not _accepts_a_root(defs[name]):
            continue
        node = defs[name]
        positional = [a.arg for a in
                      list(getattr(node.args, "posonlyargs", [])) + list(node.args.args)]
        index = next((i for i, a in enumerate(positional) if _is_data_root_param(a)),
                     None)
        out[name] = index
    return out


def _reaching(tree_of: dict[str, ast.AST]) -> tuple[set[str], dict[str, ast.AST]]:
    """The closure over the root modules, and the definitions it was built from.

    Split out so the blind set and its complement are derived from ONE walk.
    Two walks would let the two sets drift apart, and a helper belonging to
    neither is a site this tool would then report under no heading at all.
    """
    defs: dict[str, ast.AST] = {}
    for _rel, tree in tree_of.items():
        defs.update(module_functions(tree))
    return closure_over(defs, set(_SEEDS)), defs


def _functions(tree: ast.AST) -> Iterable[ast.AST]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _binding_of(func: ast.AST) -> dict[int, str]:
    """`{id(call node): the variable its value is assigned to}`, inside one body.

    The third place a base hides: bound to a name on one line and joined to the
    forwarding call on the next.
    """
    out: dict[int, str] = {}
    for node in ast.walk(func):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        if not names or node.value is None:
            continue
        for call in ast.walk(node.value):
            if isinstance(call, ast.Call):
                out[id(call)] = names[0]
    return out


def _guarded_by(func: ast.AST, param: str) -> set[int]:
    """Calls that only run when `param` was NOT supplied.

    Three spellings of the one correct pattern, and all three had to be here
    because this tree writes all three:

        root = data_root or get_data_root()
        root = data_root if data_root else get_data_root()
        if data_root is None:
            data_root = get_data_root()

    They are the DEFAULT, not the base: the caller's root wins whenever there is
    one. Reporting them would make this tool refuse the very fix it exists to
    ask for, which is the half of a guard that no refusal test ever catches.
    The statement form is the one that matters most here: MEASURED 2026-09-22,
    with only the two expression forms recognised, 8 of the 10 sites this tool
    reported were honest functions written that way.
    """
    guarded: set[int] = set()
    for node in ast.walk(func):
        if isinstance(node, (ast.BoolOp, ast.IfExp)):
            scope = node
        elif isinstance(node, ast.If):
            scope = node.test
        else:
            continue
        names = {n.id for n in ast.walk(scope) if isinstance(n, ast.Name)}
        if param not in names:
            continue
        for call in ast.walk(node):
            if isinstance(call, ast.Call):
                guarded.add(id(call))
    return guarded


def _forwards(func: ast.AST, param: str) -> list[ast.Call]:
    """Every call this body hands `param` to, by keyword or by position."""
    out = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        args = list(node.args) + [k.value for k in node.keywords]
        if any(isinstance(a, ast.Name) and a.id == param for a in args):
            out.append(node)
    return out


def _blind_calls(func: ast.AST, blind: set[str]) -> list[ast.Call]:
    read_as_string = {id(n.value) for n in ast.walk(func)
                      if isinstance(n, ast.Attribute) and n.attr in _STRING_ATTRS}
    return [n for n in ast.walk(func)
            if isinstance(n, ast.Call) and _callee(n) in blind
            and id(n) not in read_as_string]


def blind_here(tree: ast.AST, base_blind: set[str],
               tellable: dict[str, int | None] | None = None) -> set[str]:
    """`base_blind`, plus this module's own wrappers that reach a root.

    `tellable` widens the seed set, and without it this function loses a whole
    class of wrapper the moment a resolver gains a root parameter. A local
    wrapper that takes NO root is blind whether the thing it wraps can be told or
    not: its own callers still cannot say which tree they mean. Before this
    argument existed, such a wrapper fell out of the blind set along with the
    helper underneath it, and its caller's finding went with it.

    MEASURED 2026-09-25, and it is the eighth of the eight sites HOS-115's
    narrowing hid: `check-path-references.py:354` calls `in_engine_scope()`,
    which is local, takes no root, and reaches `get_routing_destination`. When
    that resolver became tellable the wrapper stopped reaching anything blind and
    the site stopped being reported at all.

    `tellable` defaults to None so the existing two-argument callers, including
    a live test that pins this signature, are unchanged.
    """
    local = module_functions(tree)
    seeds = set(base_blind) | set(tellable or {})
    blind = set(base_blind)
    for name in closure_over(local, seeds):
        # A name that IS the tellable helper is not a blind wrapper; only a
        # local function that reaches one without accepting a root is.
        if name in (tellable or {}):
            continue
        if name not in local or not _accepts_a_root(local[name]):
            blind.add(name)
    return blind


def findings_in(tree: ast.AST, blind: set[str], path: Path,
                skipped: list[str] | None = None) -> list[Finding]:
    """Every root parameter in `tree` whose base is built without it.

    `skipped` collects the engine-root parameters this deliberately does not
    judge, so the caller can print their number instead of dropping them.
    """
    skipped = [] if skipped is None else skipped
    findings: list[Finding] = []
    for func in _functions(tree):
        params = [p for p in _param_names(func) if _is_data_root_param(p)]
        skipped.extend(f"{path}:{func.lineno} {func.name}({p}=)"
                       for p in _param_names(func)
                       if _is_root_param(p) and not _is_data_root_param(p))
        if not params:
            continue
        calls = _blind_calls(func, blind)
        if not calls:
            continue
        bound = _binding_of(func)
        for param in params:
            forwards = _forwards(func, param)
            if not forwards:
                continue
            guarded = _guarded_by(func, param)
            inside = {id(n) for f in forwards for n in ast.walk(f)}
            named_in_forward = {n.id for f in forwards for n in ast.walk(f)
                                if isinstance(n, ast.Name)}
            for call in calls:
                if id(call) in guarded:
                    continue
                reaches = (id(call) in inside
                           or bound.get(id(call), "") in named_in_forward)
                findings.append(Finding(
                    path=path, line=call.lineno, func=func.name, param=param,
                    base=_callee(call),
                    forwarded_to=", ".join(sorted({_callee(f) for f in forwards})),
                    klass="direct" if reaches else "loose"))
    return findings


def unpassed_in(tree: ast.AST, tellable: dict[str, int | None],
                path: Path) -> list[Finding]:
    """Calls to a helper that accepts a root, made by a function that holds one.

    The condition is deliberately narrow, because the wide reading would flag
    every call in the tree. Both halves must be true: the CALLER has a data-root
    parameter of its own, and the CALL gives the helper no root. A caller with no
    root has nothing to pass and is not a finding; a call that already passes one
    is the repaired shape.

    A root counts as passed when it arrives by KEYWORD or when the call supplies
    enough POSITIONAL arguments to reach the parameter's index. `tellable` carries
    that index for exactly this test; reading keywords alone produced two false
    positives on the day this class was written.

    What it cannot see, stated rather than left to be found. A call whose callee
    is an attribute (`mod.helper(...)`) resolves by the final name only, so a
    same-named method on an unrelated object reads as the helper. A `*args` or
    `**kwargs` spread hides whatever it carries, and this treats such a call as
    NOT passing, so it over-reports there; the alternative is to treat a spread
    as satisfying every parameter, which would hide real sites.
    """
    findings: list[Finding] = []
    for func in _functions(tree):
        params = [p for p in _param_names(func) if _is_data_root_param(p)]
        if not params:
            continue
        for node in ast.walk(func):
            if not isinstance(node, ast.Call) or _callee(node) not in tellable:
                continue
            if any(kw.arg and _is_data_root_param(kw.arg) for kw in node.keywords):
                continue
            index = tellable[_callee(node)]
            if index is not None and len(node.args) > index:
                continue
            findings.append(Finding(
                path=path, line=node.lineno, func=func.name, param=params[0],
                base=_callee(node), forwarded_to="", klass="unpassed"))
    return findings


def _parse(path: Path) -> ast.AST | None:
    """The file's AST, or None when it cannot be read.

    One parse per file, shared by both detectors. The earlier shape parsed each
    file twice and needed a second handler that swallowed the same error in
    silence; one reader with one handler is what removes that.
    """
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return None


def scan_file(path: Path, base_blind: set[str],
              skipped: list[str] | None = None,
              tellable: dict[str, int | None] | None = None) -> list[Finding]:
    """One file's `direct`/`loose` findings. Kept as the single-file entry point."""
    tree = _parse(path)
    if tree is None:
        return []
    return findings_in(tree, blind_here(tree, base_blind, tellable),
                       path, skipped)


def _root_module_trees() -> dict[str, ast.AST]:
    """The root modules, parsed. Unreadable is a warning, never silence."""
    tree_of: dict[str, ast.AST] = {}
    for rel in _ROOT_MODULES:
        p = _ENGINE / rel
        try:
            tree_of[rel] = ast.parse(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError) as exc:
            print(f"{YELLOW}warning{RESET}: {rel} unreadable "
                  f"({type(exc).__name__}: {exc}); the blind-helper set falls "
                  f"back to the seeds and will UNDER-report.",
                  file=sys.stderr)
    return tree_of


def base_blind_helpers() -> set[str]:
    """The blind set derived from the root modules, before any local wrapper."""
    return blind_helpers(_root_module_trees())


def scan(trees: Iterable[str]) -> Scan:
    tree_of = _root_module_trees()
    base_blind = blind_helpers(tree_of)
    tellable = tellable_helpers(tree_of)

    findings: list[Finding] = []
    unpassed: list[Finding] = []
    skipped: list[str] = []
    seen: set[Path] = set()
    for rel in trees:
        root = (_ENGINE / rel).resolve()
        for path in sorted(root.rglob("*.py")) if root.is_dir() else [root]:
            if path in seen or "__pycache__" in path.parts:
                continue
            seen.add(path)
            tree = _parse(path)
            if tree is None:
                continue
            findings.extend(findings_in(
                tree, blind_here(tree, base_blind, tellable), path, skipped))
            unpassed.extend(unpassed_in(tree, tellable, path))
    return Scan(findings, skipped, len(seen), frozenset(base_blind),
                unpassed, frozenset(tellable))


def _self_test() -> int:
    """Both directions, on synthetic source, in memory.

    The refusal AND the honest case that must still pass, because a detector
    that flags everything satisfies every example of the defect.
    """
    blind = base_blind_helpers()

    cases = [
        ("the HOS-75 shape, refused",
         "def d(*, data_root=None):\n"
         "    return moved_home(get_knowledge_dir() / 'x',\n"
         "                      'build', 'runbook', data_root=data_root)\n",
         ["direct"]),
        ("the same base bound to a name first, refused",
         "def d(*, data_root=None):\n"
         "    base = get_knowledge_dir()\n"
         "    return moved_home(base / 'x', 'build', 'runbook', data_root=data_root)\n",
         ["direct"]),
        ("a wrapper three hops from a root, refused",
         "def _base():\n"
         "    return get_knowledge_dir() / 'k'\n"
         "def d(*, data_root=None):\n"
         "    return moved_home(_base(), 'build', 'runbook', data_root=data_root)\n",
         ["direct"]),
        ("the base honouring the root, PASSES",
         "def d(*, data_root=None):\n"
         "    return moved_home((data_root or get_data_root()) / 'x',\n"
         "                      'build', 'runbook', data_root=data_root)\n",
         []),
        ("a root parameter reaching the base, PASSES",
         "def d(*, data_root=None):\n"
         "    return moved_home(datastore_under(data_root) / 'x',\n"
         "                      'build', 'runbook', data_root=data_root)\n",
         []),
        ("the statement-form default, PASSES",
         "def d(*, data_root=None):\n"
         "    if data_root is None:\n"
         "        data_root = get_data_root()\n"
         "    return moved_home(Path(data_root) / 'x', 'build', 'runbook',\n"
         "                      data_root=data_root)\n",
         []),
        ("an engine-root parameter beside a data root, PASSES",
         "def d(workspace_root, *, thing=None):\n"
         "    return moved_home(get_knowledge_dir() / 'x',\n"
         "                      'build', 'runbook', data_root=workspace_root)\n",
         []),
        ("no root parameter at all, PASSES",
         "def d():\n"
         "    return get_knowledge_dir() / 'x'\n",
         []),
        ("a root parameter that is never forwarded, PASSES",
         "def d(*, data_root=None):\n"
         "    return get_knowledge_dir() / 'x'\n",
         []),
    ]
    # `unpassed` gets its own cases against its own detector, because it reads a
    # different set. A synthetic tellable helper is used rather than a real name,
    # so these stay true the day one of the four resolvers loses its parameter
    # again.
    # index 1: `resolve_with_root(name, data_root)`, so a call with two
    # positional arguments HAS passed the root.
    tellable = {"resolve_with_root": 1}
    unpassed_cases = [
        ("a caller holding a root that does not pass it, refused",
         "def d(*, data_root=None):\n"
         "    return resolve_with_root('x')\n",
         ["unpassed"]),
        ("the same call passing the root, PASSES",
         "def d(*, data_root=None):\n"
         "    return resolve_with_root('x', data_root=data_root)\n",
         []),
        ("a caller with no root of its own, PASSES",
         "def d():\n"
         "    return resolve_with_root('x')\n",
         []),
        ("an engine-root parameter is not a data root, PASSES",
         "def d(workspace_root):\n"
         "    return resolve_with_root('x')\n",
         []),
        ("a blind helper is not this class's business, PASSES",
         "def d(*, data_root=None):\n"
         "    return get_knowledge_dir()\n",
         []),
        ("the root passed POSITIONALLY, PASSES",
         "def d(*, data_root=None):\n"
         "    return resolve_with_root('x', data_root)\n",
         []),
        ("one positional short of the root, refused",
         "def d(*, data_root=None):\n"
         "    return resolve_with_root('x')\n",
         ["unpassed"]),
    ]

    failures = 0
    for label, src, want in cases:
        tmp = ast.parse(src)
        got = [f.klass for f in findings_in(tmp, blind_here(tmp, blind),
                                            Path("<self-test>"))]
        ok = got == want
        failures += 0 if ok else 1
        mark = f"{GREEN}ok{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark}  {label}: expected {want}, got {got}")
    for label, src, want in unpassed_cases:
        tmp = ast.parse(src)
        got = [f.klass for f in unpassed_in(tmp, tellable, Path("<self-test>"))]
        ok = got == want
        failures += 0 if ok else 1
        mark = f"{GREEN}ok{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark}  {label}: expected {want}, got {got}")
    total = len(cases) + len(unpassed_cases)
    print(f"{BOLD}self-test{RESET}: {total - failures}/{total} passed")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", action="append", default=[],
                    help="a tree to search, relative to the engine root "
                         "(repeatable; default: scripts/ .claude/ tests/)")
    ap.add_argument("--quiet", action="store_true",
                    help="print the counts and the direct findings only")
    ap.add_argument("--self-test", action="store_true",
                    help="run the detector against synthetic source, both ways")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()

    trees = args.only or list(_DEFAULT_TREES)
    result = scan(trees)
    direct = [f for f in result.findings if f.klass == "direct"]
    loose = [f for f in result.findings if f.klass == "loose"]

    for label, rows, color in (("direct", direct, RED), ("loose", loose, YELLOW),
                               ("unpassed", result.unpassed, YELLOW)):
        if args.quiet and label != "direct":
            continue
        if not rows:
            continue
        print(f"{BOLD}{color}{label}{RESET} ({len(rows)}):")
        for f in rows:
            rel = f.path.relative_to(_ENGINE)
            tail = (f"{GRAY}forwarded to {f.forwarded_to}{RESET}" if f.forwarded_to
                    else f"{GRAY}takes a root and was not given one{RESET}")
            print(f"  {CYAN}{rel}:{f.line}{RESET} {BOLD}{f.func}{RESET}"
                  f"({f.param}=) base {f.base}() {tail}")

    print(f"{BOLD}searched{RESET}: {', '.join(trees)} ({result.files} files, "
          f"{len(result.blind)} blind helpers, {len(result.tellable)} tellable)  "
          f"{BOLD}direct{RESET}: {len(direct)}  {BOLD}loose{RESET}: {len(loose)}  "
          f"{BOLD}unpassed{RESET}: {len(result.unpassed)}  "
          f"{GRAY}engine-root parameters not judged: {len(result.skipped)}{RESET}")
    return 1 if direct else 0


if __name__ == "__main__":
    sys.exit(main())
