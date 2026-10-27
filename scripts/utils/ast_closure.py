"""Call-graph closure over a module's top-level functions, read from the AST.

Shared by the dev checkers that ask "which helpers reach this seed":
`scripts/dev/data-root-honoured.py` and `scripts/dev/import-env-purity.py`.
They used to load these from `scripts/dev/data-path-equivalence.py` by file
path; one copy here keeps the walk identical for both.
"""
from __future__ import annotations

import ast

__all__ = ["callee", "called_names", "module_functions", "closure_over"]


def callee(call: ast.Call) -> str:
    """`get_outputs_dir` out of `get_outputs_dir(...)` or `workspace.get_outputs_dir(...)`."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def called_names(node: ast.AST) -> set[str]:
    """Every function name called anywhere inside `node`, attribute calls included."""
    return {callee(n) for n in ast.walk(node) if isinstance(n, ast.Call)} - {""}


def module_functions(tree: ast.AST) -> dict[str, ast.AST]:
    """Module-level `def`s by name. Nested ones are deliberately not reached:
    a helper a module exposes is what another module can call."""
    return {n.name: n for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def closure_over(defs: dict[str, ast.AST], seeds: set[str]) -> set[str]:
    """`seeds`, plus every function in `defs` that reaches one of them.

    A fixed point rather than one hop, because the chains in this tree are three
    deep (`context_dir` -> `get_context_dir` -> `get_data_root`) and a
    one-hop answer is the textual answer with an extra step.
    """
    reached = set(seeds)
    changed = True
    while changed:
        changed = False
        for name, node in defs.items():
            if name in reached:
                continue
            if called_names(node) & reached:
                reached.add(name)
                changed = True
    return reached
