#!/usr/bin/env python3
"""Does importing a module of this engine change the process's environment?

The shape this finds, and it is the shape HOS-95 was opened on: a module calls
`load_env()` at module scope, against a root derived from its own `__file__`.
`load_env` writes every key of that `.env` into `os.environ`, and one of those
keys is the data root. So the IMPORT repoints the data root of the whole
process, and every resolver called afterwards -- in any module, not only the one
imported -- answers about that tree instead of the one the process was given.

    # the original, .claude/skills/flux-image/scripts/generate_image.py, before HOS-95
    _workspace_root = os.path.abspath(os.path.join(_script_dir, '..', '..', '..', '..'))
    sys.path.insert(0, _workspace_root)
    from scripts.utils.workspace import load_env
    load_env(Path(_workspace_root))          # <- runs on import, sets HEADING_OS_DATA

MEASURED 2026-09-23 in a child process with `HEADING_OS_DATA` unset and
`WORKSPACE_ROOT` pointed at a fixture: `get_data_root()` answered inside the
fixture before that module was imported and outside it afterwards. It was found
as a defect of another task's measurement, where one child process out of many
was contaminated and the run looked entirely normal.

The workspace already carries a written rule about a copied `.env` silently
repointing every guard at the wrong tree. That failure needs a human to copy a
file. This one needs nobody.

## Why the AST and not an import

The direct measurement would be to import each module in a child and diff
`os.environ`. It is not what this does, and the reason is not preference:
importing every module in the tree executes every module body, which here
re-execs interpreters, opens sockets and reaches local models. A checker
that has to be run inside a sandbox is a checker nobody runs. Reading the source
costs the three blind spots named under "What this cannot see", and they are
printed by `--limits` rather than left to be discovered.

## What counts as module level

Depth-0 statements, and the bodies of module-level `if` / `try` / `with` / `for`
/ `while` / `match`, because those run on import too. NOT the body of a `def`, a
`class` or a `lambda`, which is the whole distinction a grep cannot make. NOT an
`if __name__ == "__main__":` block either: import is exactly the case in which
that guard does not fire, so flagging it would report the one place the defect
cannot be.

## What counts as a mutation

Direct: assigning, deleting, `setdefault`, `update`, `pop`, `clear` or
`__setitem__` on `os.environ`, and `os.putenv` / `os.unsetenv`.

Indirect: a call to a helper that does one of those. The set is DERIVED, never
listed, for the reason its sibling `scripts/dev/data-root-honoured.py` gives: a
list of names is a list of the names somebody thought of. Every module-level
function under `scripts/utils/` whose own body writes the environment is a seed,
and the closure over the call graph of those files adds the wrappers.

What a helper "does" is what runs when it is CALLED: its body, the decorators,
defaults and class bodies it evaluates, and any nested `def` it calls by name.
Not the body of a closure it only defines. A helper whose closure writes the
environment is DEFERRED: binding its return value at module level is not a
finding, calling that value at import is (HOS-172).

## What is exempt, and nothing else is

A finding is exempt by exactly one of two things. Anything else fails the run.

1. **One rule, for `ensure_venv()`.** A module-level call to it is allowed in a
   file that CANNOT be imported by name and that has an
   `if __name__ == "__main__":` block. Cannot be imported by name means: a
   `*.py` under `scripts/`, not under `scripts/utils/`, whose file name holds a
   hyphen, so `import` cannot spell it. The name must be bound by
   `from scripts.utils.venv_guard import ensure_venv` and by nothing else in the
   file, so a local function of the same name is not exempt. All of this is read
   from the AST, never from the text. What the call does: it removes one
   variable of its own, the anti-loop sentinel, and on a wrong interpreter it
   replaces the process. Both are the purpose of a CLI entry point, and
   `scripts/utils/venv_guard.py` tells CLI scripts to call it at module level.
   The rule does NOT make the call harmless to a process that loads such a
   script by path, which is possible without an import name: the test suite
   and `scripts/nightly-refresh.py` both do it. What protects those loaders is
   their own arrangement, not this rule: `tests/conftest.py` sets the sentinel
   before any test loads a script, and nightly-refresh runs its own
   `ensure_venv()` first. What the rule does establish is narrower: no
   `import` statement can reach the call. In an importable module every
   `import` would run it, so there it stays a finding.

2. **One entry per finding**, in `config/import-env-purity-exemptions.json`.
   The key is the file plus the sha256 of the flagged statement's text, every
   line of it, with the whitespace collapsed. The entry stores the statement
   and the checker computes the hash, so a reviewer reads the text and no
   stored hash can disagree with it. It is not the line number, which
   moves on every edit above it, and it is not the file or the function, which
   would also exempt the next write placed beside the reviewed one. It is not
   the first line alone either, because a later line of a multi-line write can
   change the value while the first line stays the same. Each entry carries a reason
   of at least `REASON_MIN_WORDS` words
   (`scripts/utils/statement_exemptions.py`, which owns the format). The reason already stands beside the
   line in the code, and the entry states it again where a reviewer of the
   exemption reads it. Two identical lines need two entries. An entry that
   matches no finding is STALE and fails the run, so the file cannot collect
   entries that guard nothing. An entry outside the searched trees is stale
   too, except on a run narrowed with `--only`. A changed statement retires
   its entry and raises a new finding.

There is no exemption for `tests/` as a directory or for the name
`ensure_venv` as such. Either one would pass the defect this check exists for:
a module-level `load_env()` added to the test conftest, or an `ensure_venv()`
put in an importable module, which is the one place its re-exec harms an
importer.

## Where it runs

It is the third push ratchet, in the table in `scripts/utils/push_ratchets.py`,
so `scripts/push-all.py` refuses an engine push on it and
`scripts/check-push-ratchets.py` shows the refusal before the merge (HOS-116,
operator decision 2026-09-25). It is not a pre-commit hook, for the reason
`legacy_outputs_ratchet` in push-all gives: a hook makes
`scripts/check-gate-integrity.py` require a test, and the test budget has no
room for a test that HOS-54 says not to write.

Usage:
  python scripts/dev/import-env-purity.py
  python scripts/dev/import-env-purity.py --raw
  python scripts/dev/import-env-purity.py --only .claude/skills --quiet
  python scripts/dev/import-env-purity.py --self-test
  python scripts/dev/import-env-purity.py --limits

Exit codes:
  0   every finding is exempt and every entry matched a finding.
  1   a finding is not exempt, or an entry is stale. With `--raw`: any finding.
  2   the exemptions file is missing or malformed, so no verdict is possible.
"""
from __future__ import annotations

import argparse
import ast
import builtins
import subprocess
import sys
from pathlib import Path
from typing import Iterable, NamedTuple

_ENGINE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ENGINE))

from scripts.utils.colors import GREEN, YELLOW, RED, CYAN, GRAY, BOLD, RESET  # noqa: E402
from scripts.utils.ast_closure import module_functions  # noqa: E402
# The exemption FORMAT is shared with the HOS-5 section 6 prohibition in
# `scripts/check-data-path-references.py`; what a finding is stays here.
from scripts.utils.statement_exemptions import (  # noqa: E402
    Entry, ExemptionsError, entries_from, line_key, load_exemptions)

#: The mapping the process's environment lives in. Matched on the ATTRIBUTE, so
#: `import os as _os; _os.environ[...]` is caught; `from os import environ` under
#: a new name is not, and that is stated under "What this cannot see".
_ENV_ATTR = "environ"

#: Methods of that mapping which change it. `get`, `items` and `__contains__`
#: read, and a checker that flagged those would refuse every honest module.
_MUTATING_METHODS = frozenset({"setdefault", "update", "pop", "clear",
                               "popitem", "__setitem__", "__delitem__"})

#: The two `os` functions that write the environment without going through the
#: mapping at all.
_OS_WRITERS = frozenset({"putenv", "unsetenv"})

#: Where the indirect seeds are read from. A directory, not a list of files: a
#: helper that starts writing the environment tomorrow is covered the same day
#: with nothing edited here.
_HELPER_TREE = "scripts/utils"

#: Names the interpreter already binds. A helper sharing one of these is
#: unreachable by a bare call; see `env_writing_helpers`.
_BUILTIN_NAMES = frozenset(dir(builtins))

_DEFAULT_TREES = ("scripts", ".claude", "tests")

#: The reviewed per-finding exemptions, relative to the engine root. Tracked, so
#: every change to it is a diff somebody reads.
_EXEMPTIONS = "config/import-env-purity-exemptions.json"

#: The one call the rule exempts, and the one module it must come from.
_RULE_CALL = "ensure_venv"
_RULE_MODULE = "scripts.utils.venv_guard"

#: The tree the rule never covers, even for a hyphenated file: helpers are
#: imported, and the rule exists only for files nothing imports by name.
_RULE_EXCLUDED = ("scripts", "utils")

_LIMITS = (
    "a mutation that happens on first CALL rather than on import. This reads "
    "module scope only, by design: that is what an importer pays for. A "
    "function that repoints the root when something finally calls it is "
    "invisible here and is a different check.",
    "a mutation that happens only under a condition. The statement is reported "
    "wherever it sits, so an `if` branch that never runs is reported and one "
    "reached through a helper's own branch is not. Depth-0 presence is what is "
    "established; execution is not.",
    "a mutation performed by a C extension, or by any code this walk does not "
    "parse. `import numpy` executes a shared object; `import` of a third-party "
    "package executes source outside this repository. Only tracked `*.py` under "
    "the searched trees is read.",
    "a mutation reached through a helper defined outside `scripts/utils/`, or "
    "through an alias (`e = os.environ; e['X'] = 1`), `setattr`, `exec`, or a "
    "name assembled at runtime. The seed set is derived from one directory and "
    "the call graph is matched by NAME.",
    "a closure returned by a deferred helper and called at import by any route "
    "other than `f()()` or a plain `x = f()` followed by `x()`: passed to "
    "another function, stored in a container, returned through a second "
    "helper. And a deferred helper is any whose nested code writes, so "
    "`f()()` is flagged even when `f` returns something else.",
)


class Finding(NamedTuple):
    path: Path
    line: int
    kind: str      # "direct" or the helper's name
    source: str    # the first line, for display
    text: str = ""  # every line of the flagged statement: what an entry keys on


class Scan(NamedTuple):
    findings: list[Finding]
    files: int
    unparsed: list[str]
    helpers: frozenset[str]
    rule_files: frozenset[Path]    # files where the `ensure_venv` rule holds
    deferred: frozenset[str] = frozenset()


class Verdict(NamedTuple):
    uncovered: list[Finding]
    by_rule: list[Finding]
    by_entry: list[Finding]
    stale: list[Entry]


def _is_env(node: ast.AST) -> bool:
    """`os.environ`, however `os` was spelled."""
    return isinstance(node, ast.Attribute) and node.attr == _ENV_ATTR


def _flat_targets(targets: Iterable[ast.AST]) -> Iterable[ast.AST]:
    """Assignment targets with tuples, lists and starred names unpacked."""
    stack = list(targets)
    while stack:
        t = stack.pop()
        if isinstance(t, (ast.Tuple, ast.List)):
            stack.extend(t.elts)
        elif isinstance(t, ast.Starred):
            stack.append(t.value)
        else:
            yield t


def _writes_env(nodes: Iterable[ast.AST]) -> bool:
    """Do these nodes contain a DIRECT write to the environment?

    A target is the mapping's item (`os.environ[k] = v`, also inside a tuple
    target) or the mapping itself (`os.environ |= {...}`, `os.environ = {}`).
    """
    for sub in nodes:
        if isinstance(sub, (ast.Assign, ast.AugAssign, ast.AnnAssign,
                            ast.Delete)):
            targets = (sub.targets if isinstance(sub, (ast.Assign, ast.Delete))
                       else [sub.target])
            for t in _flat_targets(targets):
                if _is_env(t) or (isinstance(t, ast.Subscript)
                                  and _is_env(t.value)):
                    return True
        elif isinstance(sub, ast.Call):
            f = sub.func
            if (isinstance(f, ast.Attribute) and f.attr in _MUTATING_METHODS
                    and _is_env(f.value)):
                return True
            if isinstance(f, ast.Attribute) and f.attr in _OS_WRITERS:
                return True
    return False


def _called_bare_names(nodes: Iterable[ast.AST]) -> set[str]:
    """Names this function calls WITHOUT a dot: `load_env(...)`, not `p.resolve()`.

    Its sibling `scripts/dev/data-root-honoured.py` uses `called_names` from
    `scripts/utils/ast_closure.py`, which counts an attribute call too, and that is
    right for the question it asks. It is wrong for this one, and the difference
    is measured rather than argued: three of the five seeds here are
    `scripts/utils/tracing.py`'s `mint`, `set` and `clear`, and a closure that
    counts `.set()` and `.add()` as reaching them grew the helper set to 536
    names and reported 504 findings over the tree, the bulk of them
    `Path.resolve()` (MEASURED 2026-09-23). With bare names only the set is
    small enough to read, and an attribute call still reaches this check through
    the module-alias rule in `findings_in`.
    """
    return {n.func.id for n in nodes
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _definition_time(node: ast.AST) -> list[ast.AST]:
    """The parts of a nested scope that run when it is DEFINED, not when called.

    A `def` or `lambda` evaluates its decorators and default values where it
    stands; its body waits for a call. A `class` runs its whole body at once,
    and the `def`s in that body are judged by this same rule.
    """
    if isinstance(node, ast.ClassDef):
        return [*node.decorator_list, *node.bases, *node.keywords, *node.body]
    args = node.args
    deco = [] if isinstance(node, ast.Lambda) else node.decorator_list
    return [*deco, *args.defaults, *(d for d in args.kw_defaults if d)]


def _runs_when_called(func: ast.AST,
                      outer: dict[str, ast.AST] | None = None,
                      seen: set[int] | None = None) -> list[ast.AST]:
    """Every node that executes when `func` is CALLED.

    Not the body of a nested `def` or `lambda`: defining a closure runs nothing
    inside it, so a factory that only builds and returns one reaches nothing
    through it. Until HOS-172 this walk was `ast.walk(func)`, and
    `make_run_clock` in `scripts/utils/workspace.py`, which returns a `_clock`
    that loads `.env` on first use, read as loading it itself; four
    module-level `_clock = make_run_clock()` lines were flagged as import-time
    loads while importing each module left `os.environ` unchanged (MEASURED
    2026-09-28). A nested `def` the body CALLS by name does run, so its body is
    followed, which keeps a factory that calls its own closure a finding.
    """
    seen = set() if seen is None else seen
    seen.add(id(func))
    local = dict(outer or {})
    out: list[ast.AST] = []
    stack = list(func.body)
    while stack:
        node = stack.pop()
        if isinstance(node, _SCOPES):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                local[node.name] = node
            stack.extend(_definition_time(node))
            continue
        out.append(node)
        stack.extend(ast.iter_child_nodes(node))
    for name in _called_bare_names(out):
        inner = local.get(name)
        if inner is not None and id(inner) not in seen:
            out.extend(_runs_when_called(inner, local, seen))
    return out


def _helper_sets(trees: Iterable[ast.AST]) -> tuple[set[str], set[str]]:
    """`(reached, deferred)` over the module-level functions of `trees`.

    `reached`: calling it writes the environment. `deferred`: calling it does
    not, but something defined inside it does, so what it returns may, and a
    module-level call of that return value is a finding (`make_run_clock()()`).
    `deferred` is exactly the set the walk into nested bodies used to add, so
    every call the scanner flagged before HOS-172 is still flagged whenever the
    closure actually runs at import.
    """
    defs: dict[str, ast.AST] = {}
    for tree in trees:
        defs.update(module_functions(tree))
    runs = {name: _runs_when_called(node) for name, node in defs.items()}

    def closure(nodes_of) -> set[str]:
        seeds = {name for name in defs if _writes_env(nodes_of(name))}
        # A seed that shadows a builtin propagates through the builtin instead
        # of through itself. `scripts/utils/tracing.py` defines `set`, so every
        # `set()` literal in `scripts/utils/` read as a call to it and the
        # closure grew to 254 names (MEASURED 2026-09-23; 5 seeds, 48 findings,
        # one of them `_library_roots`, which reaches no environment at all).
        # Such a helper is still reachable, and only reachable, the way it is
        # actually called: module-qualified, `trace.set(...)`, which
        # `findings_in` matches through the alias rule.
        reached = {s for s in seeds if s not in _BUILTIN_NAMES}
        changed = True
        while changed:
            changed = False
            for name in defs:
                if (name not in reached
                        and _called_bare_names(nodes_of(name)) & reached):
                    reached.add(name)
                    changed = True
        return reached | seeds

    on_call = closure(lambda name: runs[name])
    anywhere = closure(lambda name: list(ast.walk(defs[name])))
    return on_call, anywhere - on_call


def _helper_trees() -> list[ast.AST]:
    out: list[ast.AST] = []
    for path in sorted((_ENGINE / _HELPER_TREE).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            out.append(ast.parse(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
    return out


def env_writing_helpers() -> set[str]:
    """Every helper under `scripts/utils/` a module-level call must not make.

    Seeds are the functions whose body, as it runs when called, writes the
    environment; the closure adds every function in those files that reaches
    one of them, a fixed point rather than one hop because a wrapper of a
    wrapper is the ordinary shape here.
    """
    return _helper_sets(_helper_trees())[0]


def env_deferring_helpers() -> set[str]:
    """Helpers under `scripts/utils/` that return, or hold, code that writes the
    environment without running it. See `_helper_sets`."""
    return _helper_sets(_helper_trees())[1]


def _is_main_guard(node: ast.AST) -> bool:
    """`if __name__ == "__main__":` — the one block an import never runs."""
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return False
    if not isinstance(test.ops[0], ast.Eq):
        return False
    left, right = test.left, test.comparators[0]
    names = {n.id for n in (left, right) if isinstance(n, ast.Name)}
    consts = {c.value for c in (left, right) if isinstance(c, ast.Constant)}
    return "__name__" in names and "__main__" in consts


def _import_time_statements(tree: ast.AST) -> Iterable[ast.AST]:
    """Every statement an `import` of this module executes.

    Descends container statements and stops at anything that defines a scope,
    which is the distinction the whole check rests on.
    """
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            continue
        if _is_main_guard(node):
            # The guarded body never runs on import, and its `else:` always
            # does, so the `else:` is searched like any other block.
            stack.extend(node.orelse)
            continue
        yield node
        for field in ("body", "orelse", "finalbody"):
            stack.extend(getattr(node, field, []) or [])
        for handler in getattr(node, "handlers", []) or []:
            stack.extend(handler.body)
        for case in getattr(node, "cases", []) or []:
            stack.extend(case.body)


def _own_nodes(stmt: ast.AST) -> list[ast.AST]:
    """Every node `stmt` owns ITSELF: not its child statements, not a nested scope.

    A module-level `try:` and the `load_env()` inside it are two statements, and
    `_import_time_statements` yields both. Without this, the `try` reported the
    call a second time through its own body and one defect counted as two. The
    tree is never mutated to achieve that: an earlier version emptied nested
    `def` bodies in place, which silently damaged the same tree for every
    statement examined afterwards.
    """
    skip: set[int] = set()
    for field in ("body", "orelse", "finalbody"):
        for child in getattr(stmt, field, []) or []:
            skip.add(id(child))
    for group in ("handlers", "cases"):
        for child in getattr(stmt, group, []) or []:
            skip.add(id(child))
    out: list[ast.AST] = []
    stack = [stmt]
    while stack:
        node = stack.pop()
        out.append(node)
        for child in ast.iter_child_nodes(node):
            if id(child) in skip:
                continue
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef, ast.Lambda)):
                continue
            stack.append(child)
    return out


def _module_aliases(tree: ast.AST) -> set[str]:
    """Names bound by a plain `import x` / `import x as y`, anywhere in the file.

    These are the only names an attribute call can legitimately reach a helper
    through: `trace.mint()` is a module-qualified call to an engine function,
    while `p.resolve()` is a method on whatever `p` happens to be. Without the
    distinction every `.resolve()` in the tree read as a helper call.

    A `from scripts.utils import workspace` binds a module too. That form
    counts when the imported name is a module file or package of this engine,
    which is checked on disk, because the same syntax also imports functions.
    """
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.add(a.asname or a.name.split(".")[0])
        elif (isinstance(node, ast.ImportFrom) and node.level == 0
              and node.module and node.module.split(".")[0] == "scripts"):
            base = _ENGINE.joinpath(*node.module.split("."))
            for a in node.names:
                if ((base / f"{a.name}.py").is_file()
                        or (base / a.name / "__init__.py").is_file()):
                    out.add(a.asname or a.name)
    return out


def _imported_names(tree: ast.AST) -> set[str]:
    """Names a `from ... import name` binds, plus this module's own `def`s.

    A bare call is only a helper call when the name is actually bound to one
    here. Without this, a local `def set(...)` or a parameter named `clear`
    would be read as `scripts/utils/tracing`'s.
    """
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            out.update(a.asname or a.name for a in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.add(node.name)
    return out


def findings_in(tree: ast.AST, helpers: set[str], path: Path,
                source: str = "", *,
                deferred: Iterable[str]) -> list[Finding]:
    """Every import-time environment mutation in `tree`.

    `deferred` names helpers whose RETURN value writes the environment when it
    is called (`env_deferring_helpers`). Binding that value is not a finding;
    calling it at import is, as `f()()` or as `x = f()` then `x()`. It has no
    default, so a caller cannot drop it by omission: no tracked file carries
    that shape today, so nothing over the real tree would notice.
    """
    deferred = set(deferred)
    lines = source.splitlines()
    aliases = _module_aliases(tree)
    bound = _imported_names(tree)

    def _text(lineno: int) -> str:
        return lines[lineno - 1].strip() if 0 < lineno <= len(lines) else ""

    def _span(node: ast.AST) -> str:
        """Every line the flagged node covers, so a later line of a multi-line
        write changes the key. A compound statement contributes its header
        only: its body holds other statements, judged on their own."""
        end = node.end_lineno or node.lineno
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            end = max(node.lineno, body[0].lineno - 1)
        return "\n".join(_text(n) for n in range(node.lineno, end + 1))

    def _helper_called(call: ast.Call, names: set[str] = helpers) -> str:
        f = call.func
        if isinstance(f, ast.Name):
            return (f.id if f.id in names and f.id in bound
                    and f.id not in _BUILTIN_NAMES else "")
        if (isinstance(f, ast.Attribute) and f.attr in names
                and isinstance(f.value, ast.Name) and f.value.id in aliases):
            return f"{f.value.id}.{f.attr}"
        return ""

    def _deferred_called(call: ast.Call) -> str:
        f = call.func
        if isinstance(f, ast.Call):
            name = _helper_called(f, deferred)
            return f"{name}()()" if name else ""
        if isinstance(f, ast.Name) and f.id in holders:
            return f"{f.id}() from {holders[f.id]}()"
        return ""

    statements = list(_import_time_statements(tree))
    # Names bound at module level to what a deferred helper returned.
    holders: dict[str, str] = {}
    for node in statements:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            name = _helper_called(node.value, deferred)
            if name:
                holders[node.targets[0].id] = name

    out: list[Finding] = []
    for node in statements:
        owned = _own_nodes(node)
        if _writes_env(owned):
            out.append(Finding(path, node.lineno, "direct", _text(node.lineno),
                               _span(node)))
            continue
        for sub in owned:
            if not isinstance(sub, ast.Call):
                continue
            name = _helper_called(sub) or _deferred_called(sub)
            if name:
                out.append(Finding(path, sub.lineno, name, _text(sub.lineno),
                                   _span(sub)))
    return out


def _tracked(trees: Iterable[str]) -> list[Path]:
    """Tracked `*.py` under `trees`. Tracked, so a scratch file cannot fail a run."""
    try:
        proc = subprocess.run(["git", "-C", str(_ENGINE), "ls-files", "-z",
                               *[f"{t}/**/*.py" for t in trees],
                               *[f"{t}/*.py" for t in trees]],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"{YELLOW}warning{RESET}: git ls-files failed "
              f"({type(exc).__name__}: {exc}); falling back to a filesystem walk, "
              f"which also reads untracked files.", file=sys.stderr)
        return [p for t in trees for p in sorted((_ENGINE / t).rglob("*.py"))
                if "__pycache__" not in p.parts]
    seen: dict[Path, None] = {}
    for rel in proc.stdout.split("\0"):
        if rel:
            seen[_ENGINE / rel] = None
    return list(seen)


def scan(trees: Iterable[str]) -> Scan:
    helpers, deferred = _helper_sets(_helper_trees())
    findings: list[Finding] = []
    unparsed: list[str] = []
    rule_files: set[Path] = set()
    files = 0
    for path in _tracked(trees):
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, UnicodeDecodeError, SyntaxError) as exc:
            unparsed.append(f"{path.relative_to(_ENGINE)} ({type(exc).__name__})")
            continue
        files += 1
        findings.extend(findings_in(tree, helpers, path, source,
                                    deferred=deferred))
        if rule_holds(tree, path.relative_to(_ENGINE)):
            rule_files.add(path)
    return Scan(findings, files, unparsed, frozenset(helpers),
                frozenset(rule_files), frozenset(deferred))


def _not_importable_script(rel: Path) -> bool:
    """A `*.py` under `scripts/`, outside `scripts/utils/`, with a hyphen in its name."""
    parts = rel.parts
    return (len(parts) >= 2 and parts[0] == "scripts"
            and parts[:2] != _RULE_EXCLUDED
            and rel.suffix == ".py" and "-" in rel.stem)


def _rebinds_rule_call(node: ast.AST) -> bool:
    """A `def`, `class`, assignment or `del` of the name the rule exempts."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name == _RULE_CALL
    return (isinstance(node, ast.Name) and node.id == _RULE_CALL
            and isinstance(node.ctx, (ast.Store, ast.Del)))


def rule_holds(tree: ast.AST, rel: Path) -> bool:
    """Is a module-level `ensure_venv()` in this file the exempt shape?

    Three conditions, all read from the AST: the file cannot be imported by
    name, it has a module-level `__main__` block, and `ensure_venv` is bound by
    the import from `scripts.utils.venv_guard` and by nothing else. The third
    condition stops a local `def ensure_venv` that writes other variables from
    taking the exemption meant for the real one.
    """
    if not _not_importable_script(rel):
        return False
    if not any(_is_main_guard(node) for node in tree.body):
        return False
    from_guard = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if (alias.asname or alias.name) != _RULE_CALL:
                    continue
                if node.module == _RULE_MODULE and alias.asname is None:
                    from_guard = True
                else:
                    return False
        elif _rebinds_rule_call(node):
            return False
    return from_guard


def _in_trees(rel: str, trees: Iterable[str]) -> bool:
    return any(rel == t or rel.startswith(t.rstrip("/") + "/") for t in trees)


def judge(findings: list[Finding], rule_files: frozenset[Path],
          entries: list[Entry], trees: Iterable[str],
          narrowed: bool = False) -> Verdict:
    """Sort every finding into rule, entry or uncovered, and find stale entries.

    Entries and findings are matched as counts per key, not as sets: two
    identical lines in one file are two findings and need two entries, so a
    copy of a reviewed line cannot take cover under the review of the first.
    An entry outside the searched trees is stale on a full run, where no
    finding can ever reach it. Only a run narrowed with `--only` leaves it
    unjudged, because that run did not look for its finding.
    """
    trees = list(trees)
    by_rule: list[Finding] = []
    pending: dict[tuple[str, str], list[Finding]] = {}
    for f in findings:
        if f.kind == _RULE_CALL and f.path in rule_files:
            by_rule.append(f)
            continue
        rel = f.path.relative_to(_ENGINE).as_posix()
        pending.setdefault((rel, line_key(f.text or f.source)), []).append(f)
    by_entry: list[Finding] = []
    stale: list[Entry] = []
    for entry in entries:
        if narrowed and not _in_trees(entry.path, trees):
            continue
        bucket = pending.get((entry.path, entry.key))
        if bucket:
            by_entry.append(bucket.pop(0))
        else:
            stale.append(entry)
    uncovered = [f for bucket in pending.values() for f in bucket]
    return Verdict(uncovered, by_rule, by_entry, stale)


def _self_test() -> int:
    """Both directions, on synthetic source, in memory.

    The refusal AND the honest case that must still pass, because a detector
    that flags everything satisfies every example of the defect.
    """
    helpers = {"load_env", "ensure_venv"}
    cases = [
        ("the HOS-95 shape, refused",
         "from scripts.utils.workspace import load_env\n"
         "load_env(Path(_root))\n", 1),
        ("the same call inside a module-level try, refused",
         "try:\n"
         "    from scripts.utils.workspace import load_env\n"
         "    load_env(Path(_root))\n"
         "except ImportError:\n"
         "    pass\n", 1),
        ("a direct assignment at module scope, refused",
         "import os\n"
         "os.environ['HEADING_OS_DATA'] = '/somewhere'\n", 1),
        ("setdefault at module scope, refused",
         "import os\n"
         "os.environ.setdefault('X', '1')\n", 1),
        ("os.putenv at module scope, refused",
         "import os\n"
         "os.putenv('X', '1')\n", 1),
        ("the SAME call moved into a function, PASSES",
         "def token():\n"
         "    load_env(Path(_root))\n"
         "    return os.environ.get('X')\n", 0),
        ("a def inside a module-level try, PASSES",
         "try:\n"
         "    def token():\n"
         "        load_env(Path(_root))\n"
         "except ImportError:\n"
         "    pass\n", 0),
        ("a write under the __main__ guard, PASSES",
         "import os\n"
         "if __name__ == '__main__':\n"
         "    os.environ['X'] = '1'\n", 0),
        ("reading the environment at module scope, PASSES",
         "import os\n"
         "TOKEN = os.environ.get('X')\n", 0),
        ("a sys.path insert at module scope, PASSES",
         "import sys\n"
         "sys.path.insert(0, _root)\n", 0),
        ("a write in the else: of the __main__ guard, refused",
         "import os\n"
         "if __name__ == '__main__':\n"
         "    pass\n"
         "else:\n"
         "    os.environ['X'] = '1'\n", 1),
        ("os.environ |= at module scope, refused",
         "import os\n"
         "os.environ |= {'X': '1'}\n", 1),
        ("an environ item inside a tuple target, refused",
         "import os\n"
         "os.environ['X'], b = '1', 2\n", 1),
        ("a helper called through `from scripts.utils import <module>`, refused",
         "from scripts.utils import workspace\n"
         "workspace.load_env(_root)\n", 1),
    ]
    results: list[tuple[str, object, object]] = []
    for label, src, want in cases:
        got = len(findings_in(ast.parse(src), helpers, Path("<self-test>"), src,
                              deferred=()))
        results.append((label, want, got))

    # What a helper does when CALLED, derived from a synthetic `scripts/utils`
    # module (HOS-172): a closure it only defines runs nothing.
    util = ("import os\n"
            "def load_env():\n    os.environ.setdefault('X', '1')\n"
            "def make_clock():\n    def _c():\n        load_env()\n    return _c\n"
            "def make_lambda():\n    return lambda: load_env()\n"
            "def primed_clock():\n    def _c():\n        load_env()\n"
            "    _c()\n    return _c\n"
            "def builds_class():\n    class Cfg:\n        v = load_env()\n"
            "    return Cfg\n"
            "def default_arg():\n    def _c(v=load_env()):\n        return v\n"
            "    return _c\n")
    derived, deferred = _helper_sets([ast.parse(util)])
    imp = ("from scripts.utils.u import make_clock, make_lambda, primed_clock, "
           "builds_class, default_arg\n")
    called = [
        ("a factory that only returns its closure, PASSES",
         "x = make_clock()\n", 0),
        ("a factory that only returns a lambda, PASSES",
         "x = make_lambda()\n", 0),
        ("a factory that calls its own closure, refused",
         "x = primed_clock()\n", 1),
        ("a factory whose nested class body loads, refused",
         "x = builds_class()\n", 1),
        ("a factory whose nested def's default loads, refused",
         "x = default_arg()\n", 1),
        ("the returned closure called at once, refused",
         "make_clock()()\n", 1),
        ("the returned closure bound, then called, refused",
         "x = make_clock()\nx()\n", 1),
    ]
    for label, body, want in called:
        src = imp + body
        got = len(findings_in(ast.parse(src), derived, Path("<self-test>"),
                              src, deferred=deferred))
        results.append((label, want, got))

    # The exemption model. The number is what fails the run: findings no rule
    # or entry covers, plus entries that matched nothing.
    reason = "a reason long enough to be a sentence and not a fragment"

    def entry(rel: str, line: str) -> Entry:
        return Entry(rel, line, reason)

    def failing(rel: str, src: str, entries: tuple[Entry, ...] = ()) -> int:
        path = _ENGINE / rel
        tree = ast.parse(src)
        found = findings_in(tree, helpers, path, src, deferred=())
        rules = frozenset({path}) if rule_holds(tree, Path(rel)) else frozenset()
        verdict = judge(found, rules, list(entries), _DEFAULT_TREES)
        return len(verdict.uncovered) + len(verdict.stale)

    cli = ("from scripts.utils.venv_guard import ensure_venv\n"
           "ensure_venv()\n")
    main_block = "if __name__ == '__main__':\n    main()\n"
    write = "os.environ['X'] = '1'"
    conftest = "tests/conftest.py"
    judged = [
        ("ensure_venv() in a hyphenated CLI with a __main__ block, PASSES",
         "scripts/some-cli.py", cli + main_block, (), 0),
        ("the same CLI without the __main__ block, refused",
         "scripts/some-cli.py", cli, (), 1),
        ("ensure_venv() in an importable module under scripts/utils/, refused",
         "scripts/utils/some_helper.py", cli + main_block, (), 1),
        ("ensure_venv() in a hyphenated file under scripts/utils/, refused",
         "scripts/utils/some-helper.py", cli + main_block, (), 1),
        ("ensure_venv() in a snake_case script, refused",
         "scripts/some_cli.py", cli + main_block, (), 1),
        ("ensure_venv() in a hyphenated file outside scripts/, refused",
         "tests/some-case.py", cli + main_block, (), 1),
        ("a local def ensure_venv in a hyphenated CLI, refused",
         "scripts/some-cli.py",
         "import os\ndef ensure_venv():\n    os.environ['X'] = '1'\n"
         "ensure_venv()\n" + main_block, (), 1),
        ("the real import, then a local def ensure_venv, refused",
         "scripts/some-cli.py",
         "import os\nfrom scripts.utils.venv_guard import ensure_venv\n"
         "def ensure_venv():\n    os.environ['X'] = '1'\n"
         "ensure_venv()\n" + main_block, (), 1),
        ("the real import, then ensure_venv from another module, refused",
         "scripts/some-cli.py",
         "from scripts.utils.venv_guard import ensure_venv\n"
         "from scripts.utils.other import ensure_venv\n"
         "ensure_venv()\n" + main_block, (), 1),
        ("another venv_guard name imported AS ensure_venv, refused",
         "scripts/some-cli.py",
         "from scripts.utils.venv_guard import venv_python as ensure_venv\n"
         "ensure_venv()\n" + main_block, (), 1),
        ("a direct write beside an exempt ensure_venv(), refused",
         "scripts/some-cli.py", cli + "import os\n" + write + "\n" + main_block,
         (), 1),
        ("a conftest write with its entry, PASSES",
         conftest, "import os\n" + write + "\n", (entry(conftest, write),), 0),
        ("the same write with its whitespace changed, PASSES",
         conftest, "import os\nos.environ['X']   =  '1'\n",
         (entry(conftest, write),), 0),
        ("a multi-line write, entry for its first line only, refused twice",
         conftest, "import os\nos.environ['X'] = (\n    '2')\n",
         (entry(conftest, "os.environ['X'] = ("),), 2),
        ("a multi-line write with an entry for the whole statement, PASSES",
         conftest, "import os\nos.environ['X'] = (\n    '2')\n",
         (entry(conftest, "os.environ['X'] = (\n'2')"),), 0),
        ("an entry outside the searched trees on a full run, refused",
         conftest, "import os\n", (entry("docs/x.py", write),), 1),
        ("a conftest write with no entry, refused",
         conftest, "import os\n" + write + "\n", (), 1),
        ("a stale entry whose line is gone, refused",
         conftest, "import os\n", (entry(conftest, write),), 1),
        ("a changed line: a new finding AND a stale entry, refused twice",
         conftest, "import os\nos.environ['Y'] = '1'\n",
         (entry(conftest, write),), 2),
        ("two identical writes and one entry, refused once",
         conftest, "import os\n" + write + "\n" + write + "\n",
         (entry(conftest, write),), 1),
        ("an entry for another file covers nothing here, refused twice",
         conftest, "import os\n" + write + "\n",
         (entry("tests/other.py", write),), 2),
    ]
    for label, rel, src, entries, want in judged:
        results.append((label, want, failing(rel, src, entries)))

    def malformed(data: object) -> str:
        try:
            entries_from(data, "<self-test>")
        except ExemptionsError:
            return "refused"
        return "accepted"

    good = {"path": conftest, "statement": write,
            "reason": reason}
    loaded = [
        ("a well-formed entry, PASSES", {"entries": [good]}, "accepted"),
        ("an entry with no statement, refused",
         {"entries": [{k: v for k, v in good.items() if k != "statement"}]},
         "refused"),
        ("an entry whose reason is a fragment, refused",
         {"entries": [{**good, "reason": "test isolation"}]}, "refused"),
        ("an entry path with a leading ./, refused",
         {"entries": [{**good, "path": "./" + conftest}]}, "refused"),
        ("an absolute entry path, refused",
         {"entries": [{**good, "path": "/" + conftest}]}, "refused"),
        ("an entry with no reason, refused",
         {"entries": [{k: v for k, v in good.items() if k != "reason"}]},
         "refused"),
    ]
    for label, data, want in loaded:
        results.append((label, want, malformed(data)))

    failures = 0
    for label, want, got in results:
        ok = got == want
        failures += 0 if ok else 1
        mark = f"{GREEN}ok{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark}  {label}: expected {want}, got {got}")
    print(f"{BOLD}self-test{RESET}: {len(results) - failures}/{len(results)} passed")
    return 1 if failures else 0


def _print_limits() -> int:
    print(f"{BOLD}What this check cannot see{RESET}")
    for item in _LIMITS:
        print(f"  {YELLOW}-{RESET} {item}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", action="append", default=[],
                    help="a tree to search, relative to the engine root "
                         "(repeatable; default: scripts/ .claude/ tests/)")
    ap.add_argument("--quiet", action="store_true",
                    help="print the counts only")
    ap.add_argument("--raw", action="store_true",
                    help="list every finding and ignore the exemptions; "
                         "exit 1 on any finding")
    ap.add_argument("--self-test", action="store_true",
                    help="run the detector against synthetic source, both ways")
    ap.add_argument("--limits", action="store_true",
                    help="print what this method cannot see, and exit 0")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()
    if args.limits:
        return _print_limits()

    trees = args.only or list(_DEFAULT_TREES)
    entries: list[Entry] = []
    if not args.raw:
        try:
            entries = load_exemptions(_ENGINE / _EXEMPTIONS)
        except ExemptionsError as exc:
            print(f"{RED}{BOLD}exemptions unreadable{RESET}: {exc}")
            print(f"{GRAY}no verdict without a valid {_EXEMPTIONS}; "
                  f"--raw lists the findings without it{RESET}")
            return 2
    result = scan(trees)

    def show(title: str, findings: list[Finding], colour: str) -> None:
        if not findings or args.quiet:
            return
        print(f"{BOLD}{colour}{title}{RESET} ({len(findings)}):")
        for f in sorted(findings, key=lambda f: (str(f.path), f.line)):
            rel = f.path.relative_to(_ENGINE)
            print(f"  {CYAN}{rel}:{f.line}{RESET} via {BOLD}{f.kind}{RESET}"
                  f"  {GRAY}{f.source}{RESET}")

    if args.raw:
        show("import-time environment mutation", result.findings, RED)
        failed = bool(result.findings)
    else:
        verdict = judge(result.findings, result.rule_files, entries, trees,
                        narrowed=bool(args.only))
        show("import-time environment mutation, not exempt",
             verdict.uncovered, RED)
        if verdict.stale and not args.quiet:
            print(f"{BOLD}{RED}stale exemption entries{RESET} "
                  f"({len(verdict.stale)}), matching no finding in "
                  f"{_EXEMPTIONS}:")
            for e in verdict.stale:
                print(f"  {CYAN}{e.path}{RESET} {GRAY}{e.statement}{RESET}")
        if verdict.uncovered or verdict.stale:
            print(f"{GRAY}Move the write into a function, or, when the write "
                  f"is intended, add or update its entry in {_EXEMPTIONS} "
                  f"with the reason.{RESET}")
        failed = bool(verdict.uncovered or verdict.stale)

    if result.unparsed:
        print(f"{YELLOW}unparsed{RESET} ({len(result.unparsed)}): "
              f"{', '.join(result.unparsed)}")

    print(f"{BOLD}searched{RESET}: {', '.join(trees)} ({result.files} files "
          f"parsed, {len(result.unparsed)} unparsed, {len(result.helpers)} "
          f"env-writing helpers derived)  "
          f"{BOLD}findings{RESET}: {len(result.findings)}")
    if not args.raw:
        print(f"{BOLD}exempt{RESET}: {len(verdict.by_rule)} by the "
              f"{_RULE_CALL} rule, {len(verdict.by_entry)} by entry  "
              f"{BOLD}not exempt{RESET}: {len(verdict.uncovered)}  "
              f"{BOLD}stale entries{RESET}: {len(verdict.stale)}")
    print(f"{GRAY}what this cannot see: --limits{RESET}")
    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main())
