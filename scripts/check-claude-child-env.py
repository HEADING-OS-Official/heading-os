#!/usr/bin/env python3
"""Refuse any place in the engine that starts a `claude` process without `claude_child_env`.

Usage:
    .venv/bin/python scripts/check-claude-child-env.py           # list every launch, exit 1 on a finding
    .venv/bin/python scripts/check-claude-child-env.py --quiet   # verdict line only
    .venv/bin/python scripts/check-claude-child-env.py --ref main   # the committed tree of a ref

WHY (HOS-410). A `claude` child started with `--setting-sources project` never
reads `~/.claude/settings.json`, where the operator's no-telemetry switches
live, so it runs with whatever its parent's environment holds. Under a systemd
timer that is none of them. MEASURED 2026-11-22: the 01:30 nightly suite's
`rules-ab.py` auth probe created its project directory at 01:30:38.638 and
Claude Code rewrote its feature-flag cache at 01:30:38.869.
`scripts/utils/claude_child.py` forces the switches on; this check makes every
launch use it.

WHAT COUNTS AS A LAUNCH, read from the AST of every tracked `.py` file:
an argv literal (a list or tuple) whose first element is `"claude"` or a path
ending in `/claude`, a call `shutil.which("claude")`, or a name or attribute
called `claude` (any case, `claude_bin` and the like included). Such an argv is
followed to the call that receives it: directly, through a name bound to it in
the same function, or through a function in the same file that returns it.
An argv is found in a positional or a keyword argument (`args=` included).
That call must carry `env=` (or `extra_env=`) whose value is a call to
`claude_child_env`, or a name that every assignment in the same function
binds to such a call and that the function never mutates; anything else
fails closed.

FAILS CLOSED. An argv the check cannot follow to a call is a finding, not a
pass, unless `EXEMPT` below names it with a reason; an `EXEMPT` entry that
matches nothing is a finding too.

WHAT IT DOES NOT SEE. Shell scripts (`scripts/heading.sh` and
`scripts/herdr/heading-os-yard/yard-bootstrap.sh` start interactive sessions,
which read user settings and so carry the switches), a `claude` argv assembled
from pieces (`[*prefix, ...]`), and a launch through a function in another
file. A `claude` argv held in a module-level table and launched by a loop is
named in `EXEMPT` with where its launch applies the helper.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HELPER = "claude_child_env"
ENV_KEYWORDS = ("env", "extra_env")
#: A callee whose name matches starts a process (subprocess.run, Popen, os.execvp,
#: a local `_run` wrapper); an argv handed to anything else is not a launch, and
#: unless it reaches one elsewhere it is reported as one this check cannot follow.
LAUNCHER = re.compile(r"run|popen|call|exec|spawn|check_output", re.IGNORECASE)

#: (file, enclosing function or assigned module name) -> why it is not a launch here.
EXEMPT: dict[tuple[str, str], str] = {
    ("scripts/setup.py", "PREREQUISITES"):
        "a table of version checks; step_check_prereqs launches each entry through run_cmd, "
        "passing claude_child_env() for the claude row",
    ("scripts/utils/council_models.py", "VERDICT_CHOICES"):
        "the names of model families, not an argv",
    ("scripts/utils/scrutinize_record.py", "JUDGE_FAMILIES"):
        "the names of model families, not an argv",
}


def _is_claude_head(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value == "claude" or node.value.endswith("/claude")
    if isinstance(node, ast.Call):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        return (name in ("which", "_tool") and any(
            isinstance(a, ast.Constant) and a.value == "claude" for a in node.args))
    if isinstance(node, ast.Name):
        return node.id.lower().startswith("claude") and not node.id.lower().startswith("claude_md")
    if isinstance(node, ast.Attribute):
        return node.attr.lower() == "claude"
    return False


def _is_argv(node: ast.AST) -> bool:
    return (isinstance(node, (ast.List, ast.Tuple)) and bool(node.elts)
            and not isinstance(node.elts[0], ast.Starred) and _is_claude_head(node.elts[0]))


def _is_helper_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")) == HELPER


def _env_is_helper(env: ast.AST, scope: "_Scope") -> bool:
    """Does the `env=` value that reaches the launch come from the helper, unchanged?

    A direct call passes. A name passes only when EVERY binding of it in the
    function is a helper call and nothing in the function mutates it (a
    subscript write or delete, `pop`, `clear`, `popitem`, `update`,
    `setdefault`, an augmented assignment): a later `env = {}` or
    `env.pop("DISABLE_TELEMETRY")` would otherwise pass. Anything else, a
    parameter, a loop variable, an expression, fails closed.
    """
    if _is_helper_call(env):
        return True
    if not isinstance(env, ast.Name):
        return False
    bindings = scope.bound.get(env.id, [])
    if not bindings or not all(_is_helper_call(v) for v in bindings):
        return False
    for node in scope.nodes:
        if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension, ast.withitem, ast.NamedExpr)):
            target = getattr(node, "target", None) or getattr(node, "optional_vars", None)
            if target is not None and any(isinstance(n, ast.Name) and n.id == env.id for n in ast.walk(target)):
                return False
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) and node.target.id == env.id:
            return False
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Delete)):
            targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
            for t in targets:
                if (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)
                        and t.value.id == env.id):
                    return False
                if isinstance(node, ast.Assign) and isinstance(t, (ast.Tuple, ast.List)) and any(
                        isinstance(n, ast.Name) and n.id == env.id for n in ast.walk(t)):
                    return False
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == env.id
                and node.func.attr in ("pop", "clear", "popitem", "update", "setdefault", "__delitem__", "__setitem__")):
            return False
    return True


class _Scope:
    """One function body (or the module body), without the functions nested in it."""

    def __init__(self, name: str, body: list[ast.stmt]):
        self.name = name
        self.nodes: list[ast.AST] = []
        stack = list(body)
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue  # its own scope
            self.nodes.append(node)
            for child in ast.iter_child_nodes(node):
                if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    stack.append(child)
        self.bound: dict[str, list[ast.AST]] = {}
        for node in self.nodes:
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name):
                        self.bound.setdefault(t.id, []).append(node.value)


def _scopes(tree: ast.Module) -> list[_Scope]:
    out = [_Scope("<module>", tree.body)]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(_Scope(node.name, node.body))
    return out


def _module_names(tree: ast.Module) -> dict[int, str]:
    """id(argv node) -> the module-level name it is assigned under, for EXEMPT."""
    out = {}
    for stmt in tree.body:
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None:
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if names:
                for sub in ast.walk(stmt.value):
                    out[id(sub)] = names[0]
    return out


def check_file(rel: str, source: str) -> tuple[list[str], list[str], set[tuple[str, str]]]:
    """(launches verified, findings, EXEMPT keys used) for one file."""
    try:
        tree = ast.parse(source, filename=rel)
    except SyntaxError as exc:
        return [], [f"{rel}: cannot be parsed ({exc.msg}), so its launches cannot be checked"], set()
    scopes = _scopes(tree)
    module_names = _module_names(tree)
    argvs = {id(n): (n, s) for s in scopes for n in s.nodes if _is_argv(n)}
    if not argvs:
        return [], [], set()

    def is_claude_value(value: ast.AST, scope: _Scope, builders: set[str], depth: int = 0) -> list[ast.AST]:
        """The argv nodes `value` evaluates to, as far as this file shows."""
        if depth > 4:
            return []
        if _is_argv(value):
            return [value]
        if isinstance(value, ast.Name):
            return [a for v in scope.bound.get(value.id, []) for a in is_claude_value(v, scope, builders, depth + 1)]
        if isinstance(value, ast.Call):
            func = value.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in builders:
                return list(builders_argv[name])
        return []

    # Functions that return a claude argv, for a launch one call away.
    builders: set[str] = set()
    builders_argv: dict[str, list[ast.AST]] = {}
    for scope in scopes[1:]:
        for node in scope.nodes:
            if isinstance(node, ast.Return) and node.value is not None:
                found = is_claude_value(node.value, scope, set())
                if found:
                    builders.add(scope.name)
                    builders_argv.setdefault(scope.name, []).extend(found)

    reached: dict[int, list[str]] = {}
    verified, findings = [], []
    for scope in scopes:
        for node in scope.nodes:
            if not isinstance(node, ast.Call) or _is_helper_call(node):
                continue
            func = node.func
            if not LAUNCHER.search(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")):
                continue
            carried = [*node.args, *(k.value for k in node.keywords
                                     if k.arg not in (*ENV_KEYWORDS, "executable"))]
            hits = [a for arg in carried for a in is_claude_value(arg, scope, builders)]
            if not hits:
                continue
            exe = next((k.value for k in node.keywords if k.arg == "executable"), None)
            if isinstance(exe, ast.Constant) and isinstance(exe.value, str) and not _is_claude_head(exe):
                for a in hits:  # another program run under the name claude (a test's stand-in process)
                    reached.setdefault(id(a), []).append(f"{rel}:{node.lineno} executable={exe.value}")
                continue
            env = next((k.value for k in node.keywords if k.arg in ENV_KEYWORDS), None)
            ok = env is not None and _env_is_helper(env, scope)
            where = f"{rel}:{node.lineno} in {scope.name}()"
            for a in hits:
                reached.setdefault(id(a), []).append(where)
            if ok:
                verified.append(where)
            else:
                findings.append(f"{where}: starts claude without env={HELPER}(...)")
    used: set[tuple[str, str]] = set()
    for key, (node, scope) in argvs.items():
        if key in reached:
            continue
        label = module_names.get(key, scope.name)
        if (rel, label) in EXEMPT:
            used.add((rel, label))
            continue
        findings.append(f"{rel}:{node.lineno} in {label}: a claude argv this check cannot follow to its launch")
    return verified, findings, used


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True,
                          check=True, errors="replace").stdout


def tracked_python(ref: str | None) -> list[str]:
    out = _git("ls-tree", "-r", "-z", "--name-only", ref) if ref else _git("ls-files", "-z")
    return [p for p in out.split("\0") if p.endswith(".py")]


def read_source(rel: str, ref: str | None) -> str | None:
    if ref:
        return _git("show", f"{ref}:{rel}")
    path = REPO / rel
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--quiet", action="store_true", help="print the verdict line only")
    ap.add_argument("--ref", help="check the committed tree of this git ref instead of the working tree")
    args = ap.parse_args()
    files = tracked_python(args.ref)
    if not files:
        print(f"REFUSED: git found no Python file in {args.ref or REPO}", file=sys.stderr)
        return 2
    verified, findings, used = [], [], set()
    for rel in files:
        source = read_source(rel, args.ref)
        if source is None or "claude" not in source.lower():
            continue  # no claude head can appear in a file that never spells the word
        v, f, u = check_file(rel, source)
        verified += v
        findings += f
        used |= u
    findings += [f"EXEMPT entry {k} matches nothing; remove it" for k in EXEMPT if k not in used]
    if not args.quiet:
        for line in verified:
            print(f"ok    {line}")
        for line in findings:
            print(f"FAIL  {line}")
    print(f"claude child env: {len(files)} Python files, {len(verified)} launch(es) with {HELPER}, "
          f"{len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
