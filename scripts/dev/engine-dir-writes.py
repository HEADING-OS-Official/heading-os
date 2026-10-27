#!/usr/bin/env python3
"""Does the engine spell an address INSIDE its own checkout that git ignores?

HOS-5 section 6 asks for a check that forbids writing artifacts into the Engine.
Section 7, run on 2026-09-25, found why the existing walls had not: the engine
wrote logs, scratch, hook state, caches, daemon pid files and push status into
its own checkout, under `.logs/`, `.tmp/`, `.claude/state/`, `.cache/`, `.sync/`,
`.daemon-state/`, `.sync-exchange/` and `.push-state/`. Every one of those is
gitignored, and an ignored path is invisible to `scripts/leak-guard.py`, to
`tests/test_engine_tree_clean.py` and to the push-time content scan, which all
select over `git ls-files` plus the untracked-and-not-ignored set. The
operator's decision that day, verbatim: "old way should go out and do not stay
in the workspace." This is the check that keeps it out.

    python scripts/dev/engine-dir-writes.py            # the gate: exit 1 on a finding
    python scripts/dev/engine-dir-writes.py --json     # every finding, exempt or not

## The question, and why it is asked of git

A FINDING is an address built from THIS checkout's root plus a literal tail,
where git says the tail is IGNORED here. Not a list of directory names: a list
of names is a list of the names somebody thought of, and the next writer picks a
new one. Every place an artifact can hide in this checkout without the other
walls seeing it is, by construction, a path the checkout's own `.gitignore`
ignores, so the checkout's own `git check-ignore` is the definition. A tracked
source file (`scripts/x.py`, `config/y.json`) is not ignored and never a finding;
a new hiding place needs a new `.gitignore` line, and the line makes it one.

Asked of the checkout being scanned, derived from `--root` or from this file's
own location, never from a constant (CLAUDE.md, "Guards must be armed inside the
task").

## What "built from this checkout's root" means

Read from the AST, per module and per function, flow-insensitive:

  * `get_workspace_root()`, `engine_root()`, `Path(os.environ["CLAUDE_PROJECT_DIR"])`
  * `Path(__file__)` and `os.path.abspath(__file__)` walked up with `.parent`,
    `.parents[k]`, `.resolve()` or `os.path.dirname`, landing at the checkout
    root or inside it (a file three levels down that climbs two is still in the
    checkout, and the tail is joined to where it landed)
  * a name assigned one of those, or assigned such a name joined with literals
  * `/`, `.joinpath(...)`, `os.path.join(...)` and `Path(a, b)` with literal tails

In shell scripts, a line that joins `$WORKSPACE`, `$WORKSPACE_ROOT`, `$ROOT`,
`$REPO_ROOT`, `$ENGINE_ROOT`, `$CLAUDE_PROJECT_DIR` or `$(pwd)` with a literal.

A read is a finding as well as a write: an address spelled into the checkout is
the old way whichever direction the bytes go.

## What is exempt, and nothing else is

A reviewed entry in `config/engine-dir-writes-exemptions.json`, keyed by the
file and the sha256 of the statement (`scripts/utils/statement_exemptions.py`,
the format two other gates share), with a reason of at least eight words. An
entry matching nothing is stale and refuses too, so a review cannot outlive the
code it reviewed. There is no field for a file or a directory.

## What this cannot see, stated rather than implied

A tail that is not a literal (an f-string, a variable, a loop over a table), a
root that arrives as a parameter, a write handed to another process as an
argument, a Markdown snippet in a skill, and a path assembled across modules by
anything other than an imported name. Each is a hole a determined writer can
use; the rule this enforces is the ordinary way of writing an address here.

Exit 0 clean, 1 a finding no entry covers or a stale entry, 2 unusable input.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

ENGINE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ENGINE_ROOT))

from scripts.utils.statement_exemptions import (  # noqa: E402
    ExemptionsError,
    line_key,
    load_exemptions,
    match,
)

EXEMPTIONS_REL = "config/engine-dir-writes-exemptions.json"

#: Calls that answer this checkout's root. Named because they are the engine's
#: own root resolvers; a new one is a new name here, and until then a gap.
ROOT_CALLS = frozenset({"get_workspace_root", "engine_root"})

#: Shell variables that hold this checkout's root in the engine's scripts.
SHELL_ROOT = re.compile(
    r"(?:\$\{?(?:WORKSPACE|WORKSPACE_ROOT|ROOT|REPO_ROOT|ENGINE_ROOT|"
    r"CLAUDE_PROJECT_DIR)\}?|\$\(pwd\))/([A-Za-z0-9_.][A-Za-z0-9_./-]*)")

#: Top-level ignored names that hold TOOLING, never an artifact. One, and it is
#: here by its reason, not by its spelling: `.venv` is the pinned interpreter uv
#: builds from `uv.lock`, and every script that runs another names
#: `.venv/bin/python` inside it. Nothing the engine writes lands there. A second
#: row is a design change to argue in review, not a way past a finding.
TOOLING = frozenset({".venv"})

#: Where this looks. Tests are included: a fixture that writes into the checkout
#: is the same write.
PY_GLOBS = ("scripts/**/*.py", ".claude/**/*.py", "tests/**/*.py")
SH_GLOBS = ("scripts/**/*.sh", ".claude/**/*.sh", ".githooks/*")

_UNKNOWN = object()


# ============================================================
# Python: rootedness, as a relative path or unknown
# ============================================================

def _const_str(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _join(base: str, tail: str) -> str | None:
    """`base/tail` relative to the checkout, or None when the tail leaves it."""
    if PurePosixPath(tail).is_absolute():
        return None
    parts = [p for p in PurePosixPath(tail).parts if p not in ("", ".")]
    if ".." in parts:
        return None
    return "/".join([p for p in base.split("/") if p] + parts)


class _Module:
    """One file's names that hold a path inside the checkout, as relative paths."""

    def __init__(self, rel_file: str, tree: ast.AST):
        self.file_dir = str(PurePosixPath(rel_file).parent)
        if self.file_dir == ".":
            self.file_dir = ""
        self.tree = tree

    # A value is "" for the checkout root, "a/b" for a directory in it, None when
    # the expression is not rooted in the checkout, or _UNKNOWN.
    def value(self, node, env: dict):
        if isinstance(node, ast.Name):
            return env.get(node.id)
        if isinstance(node, ast.Call):
            return self._call(node, env)
        if isinstance(node, ast.Attribute):
            if node.attr == "parent":
                return self._up(self.value(node.value, env), 1)
            return None
        if isinstance(node, ast.Subscript):
            if (isinstance(node.value, ast.Attribute) and node.value.attr == "parents"
                    and isinstance(node.slice, ast.Constant)
                    and isinstance(node.slice.value, int)):
                return self._up(self.value(node.value.value, env), node.slice.value + 1)
            return None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            base = self.value(node.left, env)
            tail = _const_str(node.right)
            if isinstance(base, str) and tail is not None:
                return _join(base, tail)
            return None
        return None

    def _file(self):
        # `__file__` itself: a FILE, so its first `.parent` is its directory.
        return ("__file__",)

    def _up(self, val, n: int):
        if isinstance(val, tuple) and val == ("__file__",):
            n -= 1
            val = self.file_dir
        if not isinstance(val, str):
            return None
        parts = [p for p in val.split("/") if p]
        if n > len(parts):
            return None
        return "/".join(parts[: len(parts) - n]) if n else val

    def _call(self, node: ast.Call, env: dict):
        func = node.func
        name = func.id if isinstance(func, ast.Name) else (
            func.attr if isinstance(func, ast.Attribute) else None)
        if name in ROOT_CALLS and not node.args:
            return ""
        if name == "Path" or name == "PurePath":
            if not node.args:
                return None
            first = node.args[0]
            if isinstance(first, ast.Name) and first.id == "__file__":
                base = ("__file__",)
            elif self._is_project_dir_env(first):
                base = ""
            else:
                base = self.value(first, env)
            for extra in node.args[1:]:
                tail = _const_str(extra)
                if not isinstance(base, str) or tail is None:
                    return None if not isinstance(base, tuple) else None
                base = _join(base, tail)
            return base
        if isinstance(func, ast.Attribute) and func.attr in ("resolve", "absolute", "expanduser"):
            return self.value(func.value, env)
        if isinstance(func, ast.Attribute) and func.attr == "joinpath":
            base = self.value(func.value, env)
            for arg in node.args:
                tail = _const_str(arg)
                if not isinstance(base, str) or tail is None:
                    return None
                base = _join(base, tail)
            return base
        if (isinstance(func, ast.Attribute) and func.attr in ("abspath", "realpath")
                and node.args and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "__file__"):
            return ("__file__",)
        if (isinstance(func, ast.Attribute) and func.attr in ("abspath", "realpath")
                and node.args):
            return self.value(node.args[0], env)
        if isinstance(func, ast.Attribute) and func.attr == "dirname" and node.args:
            return self._up(self.value(node.args[0], env), 1)
        if isinstance(func, ast.Attribute) and func.attr == "join" and node.args:
            base = self.value(node.args[0], env)
            for arg in node.args[1:]:
                tail = _const_str(arg)
                if not isinstance(base, str) or tail is None:
                    return None
                base = _join(base, tail)
            return base
        if name == "str" and node.args:
            return self.value(node.args[0], env)
        return None

    @staticmethod
    def _is_project_dir_env(node) -> bool:
        # os.environ["CLAUDE_PROJECT_DIR"] or os.environ.get("CLAUDE_PROJECT_DIR", ...)
        if isinstance(node, ast.Subscript) and _const_str(node.slice) == "CLAUDE_PROJECT_DIR":
            return True
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("get", "getenv") and bool(node.args)
                and _const_str(node.args[0]) == "CLAUDE_PROJECT_DIR")

    def environments(self):
        """(scope node, env) for the module and every function, names resolved."""
        module_env: dict = {}
        self._collect(self.tree.body, module_env)
        yield self.tree, module_env
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                env = dict(module_env)
                self._collect(node.body, env)
                yield node, env

    def _collect(self, body, env: dict) -> None:
        # Two passes so a name assigned from a later-defined root still resolves.
        for _ in range(2):
            for stmt in body:
                for node in ast.walk(stmt):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                         ast.ClassDef)) and node is not stmt:
                        continue
                    if isinstance(node, ast.Assign) and len(node.targets) == 1:
                        target = node.targets[0]
                        if isinstance(target, ast.Name):
                            val = self.value(node.value, env)
                            if isinstance(val, str):
                                env[target.id] = val
                    elif isinstance(node, ast.AnnAssign) and node.value is not None:
                        if isinstance(node.target, ast.Name):
                            val = self.value(node.value, env)
                            if isinstance(val, str):
                                env[node.target.id] = val


def _joins_a_literal(node) -> bool:
    """Whether `node` is where a literal tail is JOINED to a path.

    Only those are findings. Passing an already-built address on (`str(p)`,
    `Path(p)`, `p.resolve()`) is the same address used again, and reporting
    every use would make one address as many findings as it has readers.
    """
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _const_str(node.right) is not None
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.id if isinstance(func, ast.Name) else (
        func.attr if isinstance(func, ast.Attribute) else None)
    if name in ("joinpath", "join"):
        return any(_const_str(a) is not None for a in node.args[1 if name == "join" else 0:])
    if name in ("Path", "PurePath"):
        return len(node.args) > 1 and any(_const_str(a) is not None for a in node.args[1:])
    return False


def _enclosing_statement(tree: ast.AST, target: ast.AST):
    best = None
    for node in ast.walk(tree):
        simple = isinstance(node, ast.stmt) and not isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                   ast.If, ast.For, ast.While, ast.Try, ast.With))
        spans = simple and node.lineno <= target.lineno and (
            (node.end_lineno or node.lineno) >= (target.end_lineno or target.lineno))
        if spans and (best is None or node.lineno >= best.lineno):
            best = node
    return best


def python_candidates(root: Path, rel_file: str) -> list[dict]:
    source = (root / rel_file).read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    mod = _Module(rel_file, tree)
    out: dict[tuple[int, str], dict] = {}
    for scope, env in mod.environments():
        body = scope.body if hasattr(scope, "body") else []
        for stmt in body:
            for node in ast.walk(stmt):
                if scope is tree and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not _joins_a_literal(node):
                    continue
                val = mod.value(node, env)
                if not isinstance(val, str) or not val:
                    continue
                # Only the OUTERMOST rooted expression names the address; its
                # inner pieces are prefixes of it and would be the same finding.
                owner = _enclosing_statement(tree, node)
                if owner is None:
                    continue
                text = ast.get_source_segment(source, owner) or ""
                key = (owner.lineno, val)
                out[key] = {"path": rel_file, "line": owner.lineno,
                            "statement": text, "address": val}
    # Drop an address that is a strict prefix of another from the same statement.
    rows = list(out.values())
    keep = []
    for row in rows:
        longer = [o for o in rows if o["line"] == row["line"]
                  and o["address"].startswith(row["address"] + "/")]
        if not longer:
            keep.append(row)
    return keep


def shell_candidates(root: Path, rel_file: str) -> list[dict]:
    rows = []
    text = (root / rel_file).read_text(encoding="utf-8", errors="replace")
    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        for m in SHELL_ROOT.finditer(line):
            address = m.group(1).rstrip("/")
            rows.append({"path": rel_file, "line": number,
                         "statement": line.strip(), "address": address})
    return rows


# ============================================================
# git decides what is ignored
# ============================================================

def ignored(root: Path, addresses: list[str]) -> set[str]:
    """The addresses this checkout's git ignores, file or directory alike.

    Each is asked twice, as itself and with a child appended, because a pattern
    written `.logs/` matches only a DIRECTORY and git cannot know an address
    that does not exist yet is one. A tracked path is never reported ignored.
    """
    probes = {}
    for a in addresses:
        if a.split("/")[0] in TOOLING:
            continue
        # git refuses, fatally, to look beyond a symbolic link, so a probe stops
        # at the first component that is one.
        parts = a.split("/")
        stop = len(parts)
        for i in range(1, len(parts) + 1):
            if (root / "/".join(parts[:i])).is_symlink():
                stop = i
                break
        asked = "/".join(parts[:stop])
        probes[asked] = a
        if stop == len(parts) and not (root / asked).is_symlink():
            probes[asked + "/__engine_dir_writes_probe__"] = a
    proc = subprocess.run(  # nosec B603 B607 - fixed argv, no shell
        ["git", "-C", str(root), "check-ignore", "--stdin"],
        input="\n".join(probes) + "\n", capture_output=True, text=True, check=False)
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"git check-ignore failed: {proc.stderr.strip()}")
    return {probes[line] for line in proc.stdout.splitlines() if line in probes}


def tracked(root: Path, globs: tuple[str, ...]) -> list[str]:
    proc = subprocess.run(  # nosec B603 B607 - fixed argv, no shell
        # `:(glob)`, or git reads `**` as two `*` and `scripts/**/*.py` misses
        # every file directly under `scripts/`. MEASURED 2026-09-25: 249 files
        # without the magic, 497 with it.
        ["git", "-C", str(root), "ls-files", "-z", "--",
         *(f":(glob){g}" for g in globs)],
        capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"git ls-files failed: {proc.stderr.strip()}")
    return sorted(p for p in proc.stdout.split("\0") if p)


def scan(root: Path) -> list[dict]:
    rows: list[dict] = []
    for rel in tracked(root, PY_GLOBS):
        if rel == "scripts/dev/engine-dir-writes.py":
            continue  # this file names the question, and answers nothing
        rows.extend(python_candidates(root, rel))
    for rel in tracked(root, SH_GLOBS):
        path = root / rel
        if path.suffix in (".py",) or not path.is_file():
            continue
        rows.extend(shell_candidates(root, rel))
    hidden = ignored(root, sorted({r["address"] for r in rows}))
    return [r for r in rows if r["address"] in hidden]


# ============================================================
# CLI
# ============================================================

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", type=Path, default=ENGINE_ROOT,
                    help="the engine checkout to scan (default: this one)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    root = args.root.resolve()
    try:
        findings = scan(root)
        entries = load_exemptions(root / EXEMPTIONS_REL)
    except (OSError, RuntimeError, ExemptionsError) as exc:
        print(f"engine-dir-writes: cannot judge this tree: {exc}", file=sys.stderr)
        return 2
    keys = [(f["path"], line_key(f["statement"])) for f in findings]
    uncovered, stale = match(keys, entries)
    uncovered_set = set(uncovered)
    open_findings = [f for f, k in zip(findings, keys, strict=True) if k in uncovered_set]
    if args.json:
        json.dump({"findings": findings, "uncovered": open_findings,
                   "stale": [e._asdict() for e in stale],
                   "exempted": len(findings) - len(open_findings)},
                  sys.stdout, indent=2)
        sys.stdout.write("\n")
    for f in open_findings:
        print(f"{f['path']}:{f['line']}: an address inside the engine checkout "
              f"that git ignores: {f['address']}", file=sys.stderr)
    for e in stale:
        print(f"{EXEMPTIONS_REL}: stale entry for {e.path}: it matches no "
              f"statement any more", file=sys.stderr)
    print(f"engine-dir-writes: {len(findings)} finding(s), "
          f"{len(findings) - len(open_findings)} exempted with a reason, "
          f"{len(open_findings)} unexplained, {len(stale)} stale entr(y/ies)",
          file=sys.stderr)
    return 1 if open_findings or stale else 0


if __name__ == "__main__":
    sys.exit(main())
