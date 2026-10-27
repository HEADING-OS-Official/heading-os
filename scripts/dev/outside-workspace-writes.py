#!/usr/bin/env python3
"""Refuse engine code that writes a Workspace artifact outside the Workspace.

Usage:
    python scripts/dev/outside-workspace-writes.py            # the gate: exit 1 on a finding
    python scripts/dev/outside-workspace-writes.py --raw      # every finding, exemptions ignored
    python scripts/dev/outside-workspace-writes.py --root DIR # scan another engine tree
    python scripts/dev/outside-workspace-writes.py --limits   # what this cannot see

HOS-5 § 1: no artifact that belongs to the Workspace is stored outside it, and
the operator's rule forbids scratch in `/tmp`. MEASURED 2026-09-25 on the tree
at `d278a1c0`: 20 `tempfile` calls under `scripts/` and `.claude/` created
their file or directory in the system temp directory, among them a copy of the
browser cookie store and a script holding the router password, and one debug
trace of raw mail bodies fell back to `tempfile.gettempdir()`. Every one of them
now names a Workspace directory, and this is what keeps the next one out.

WHAT IS A FINDING, asked of the AST of every tracked `.py` under `scripts/` and
`.claude/` (tests excluded: `tests/` holds pytest's own temp handling):

  temp-without-dir  a `tempfile` creation call (`mkstemp`, `mkdtemp`, `mktemp`,
                    `NamedTemporaryFile`, `TemporaryFile`,
                    `SpooledTemporaryFile`, `TemporaryDirectory`) with no
                    directory, by keyword or position, or with `dir=None`.
  system-tempdir    a call to `tempfile.gettempdir()` or `gettempdirb()`.
  tmp-literal       a string constant that IS `/tmp` or `/var/tmp` or starts
                    with either followed by `/`. Docstrings are not read.

A `dir=` argument is accepted whatever its expression: this cannot evaluate
one, and the common honest forms (`dir=path.parent` for an atomic write beside
its target, `dir=data_write_dir("runtime:<service>/tmp")`) are both
expressions. `dir=tempfile.gettempdir()` is still refused, by its own rule.

A finding is exempt only by a reviewed entry in
`config/outside-workspace-writes-exemptions.json`, the format
`scripts/utils/statement_exemptions.py` owns: the file, the statement's text,
and a reason of at least eight words. An entry that matches nothing refuses
too, so a review cannot outlive the code it reviewed.

Wired as a push ratchet in `scripts/utils/push_ratchets.py`, so
`scripts/push-all.py` and `scripts/check-push-ratchets.py` both run it.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.utils.colors import BOLD, CYAN, GRAY, RED, RESET  # noqa: E402
from scripts.utils.statement_exemptions import (  # noqa: E402
    Entry, ExemptionsError, line_key, load_exemptions, match,
)

_ENGINE = Path(__file__).resolve().parent.parent.parent
EXEMPTIONS_REL = "config/outside-workspace-writes-exemptions.json"
DEFAULT_TREES = ("scripts", ".claude")

#: Creation call -> index of its positional `dir` parameter (CPython 3.11).
CREATORS = {
    "mkstemp": 2, "mkdtemp": 2, "mktemp": 2, "TemporaryDirectory": 2,
    "NamedTemporaryFile": 6, "TemporaryFile": 6, "SpooledTemporaryFile": 7,
}
TEMPDIR_CALLS = {"gettempdir", "gettempdirb"}
SYSTEM_TEMP_ROOTS = ("/tmp", "/var/tmp")  # noqa: S108  # nosec B108 - compared, never written


class Finding(NamedTuple):
    path: str       # relative, POSIX
    line: int
    kind: str
    statement: str  # the enclosing statement's source, what an entry keys on


def _tracked(root: Path, trees: tuple[str, ...]) -> list[str]:
    """Tracked `.py` files under `trees`, from git; refuse rather than guess."""
    proc = subprocess.run(  # nosec B603 B607 - fixed argv, no shell
        ["git", "-C", str(root), "ls-files", "-z", "--", *trees],
        capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise SystemExit(f"git ls-files failed under {root}: {proc.stderr.strip()}")
    return sorted(p for p in proc.stdout.split("\0") if p.endswith(".py"))


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def _tempfile_names(tree: ast.AST) -> tuple[set[str], dict[str, str]]:
    """Module aliases of `tempfile`, and bare names bound to its functions."""
    modules, bare = {"tempfile"}, {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "tempfile":
                    modules.add(a.asname or "tempfile")
        elif isinstance(node, ast.ImportFrom) and node.module == "tempfile":
            for a in node.names:
                bare[a.asname or a.name] = a.name
    return modules, bare


def _tempfile_function(call: ast.Call, modules: set[str], bare: dict[str, str]) -> str | None:
    f = call.func
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in modules:
        return f.attr
    if isinstance(f, ast.Name) and f.id in bare:
        return bare[f.id]
    return None


def _has_dir(call: ast.Call, position: int) -> bool:
    for kw in call.keywords:
        if kw.arg == "dir":
            return not (isinstance(kw.value, ast.Constant) and kw.value.value is None)
        if kw.arg is None:  # **kwargs: cannot be read, so it is not a directory
            return False
    if len(call.args) > position:
        arg = call.args[position]
        return not (isinstance(arg, ast.Constant) and arg.value is None)
    return False


def _is_system_temp(value: str) -> bool:
    return any(value == r or value.startswith(r + "/") for r in SYSTEM_TEMP_ROOTS)


def _statements(tree: ast.AST) -> dict[int, ast.stmt]:
    """id(node) -> the innermost statement holding it."""
    owner: dict[int, ast.stmt] = {}

    def visit(node: ast.AST, stmt: ast.stmt | None) -> None:
        here = node if isinstance(node, ast.stmt) else stmt
        if here is not None:
            owner[id(node)] = here
        for child in ast.iter_child_nodes(node):
            visit(child, here)

    visit(tree, None)
    return owner


def _statement_text(source: str, stmt: ast.stmt) -> str:
    # A compound statement keys on its header line only, so an edit deep in its
    # body does not stale an entry about the header.
    if isinstance(stmt, (ast.If, ast.For, ast.While, ast.With, ast.Try, ast.FunctionDef,
                         ast.AsyncFunctionDef, ast.ClassDef, ast.AsyncWith, ast.AsyncFor)):
        return source.splitlines()[stmt.lineno - 1]
    return ast.get_source_segment(source, stmt) or source.splitlines()[stmt.lineno - 1]


def findings_in(source: str, rel: str) -> list[Finding]:
    tree = ast.parse(source)
    docs = _docstring_ids(tree)
    modules, bare = _tempfile_names(tree)
    owner = _statements(tree)
    out: list[Finding] = []

    def add(node: ast.AST, kind: str) -> None:
        stmt = owner.get(id(node))
        text = _statement_text(source, stmt) if stmt is not None else ""
        out.append(Finding(rel, node.lineno, kind, text))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _tempfile_function(node, modules, bare)
            if name in CREATORS and not _has_dir(node, CREATORS[name]):
                add(node, "temp-without-dir")
            elif name in TEMPDIR_CALLS:
                add(node, "system-tempdir")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docs and _is_system_temp(node.value):
            add(node, "tmp-literal")
    return out


def scan_tree(root: Path, trees: tuple[str, ...]) -> tuple[list[Finding], int]:
    findings: list[Finding] = []
    files = _tracked(root, trees)
    for rel in files:
        findings.extend(findings_in((root / rel).read_text(encoding="utf-8"), rel))
    return findings, len(files)


LIMITS = """What this cannot see:
  - shell scripts, markdown instructions and skill prose (a `/tmp` path an
    agent is TOLD to write is prose; the skills were fixed by hand 2026-09-25);
  - a directory computed at run time: any `dir=` expression is accepted;
  - HOME writers (`Path.home()`, `expanduser`), which include legitimate
    installers of systemd units and third-party caches;
  - a child process's own temp files (node, chromium, git), which follow
    `TMPDIR` in the child's environment;
  - tests/, excluded on purpose."""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=_ENGINE,
                    help="the engine tree to scan (default: this checkout)")
    ap.add_argument("--raw", action="store_true",
                    help="list every finding and ignore the exemptions; exit 1 on any")
    ap.add_argument("--limits", action="store_true", help="print what this cannot see")
    args = ap.parse_args(argv)
    if args.limits:
        print(LIMITS)
        return 0

    root = args.root.resolve()
    findings, n_files = scan_tree(root, DEFAULT_TREES)
    if n_files == 0:
        # A floor, not a pass: an empty corpus would report clean.
        print(f"{RED}no tracked .py under {', '.join(DEFAULT_TREES)} in {root}; refusing{RESET}")
        return 2

    if args.raw:
        for f in findings:
            print(f"  {CYAN}{f.path}:{f.line}{RESET} {BOLD}{f.kind}{RESET} {GRAY}{f.statement.strip()[:120]}{RESET}")
        print(f"{BOLD}searched{RESET}: {n_files} files  {BOLD}findings{RESET}: {len(findings)}")
        return 1 if findings else 0

    try:
        entries: list[Entry] = load_exemptions(root / EXEMPTIONS_REL)
    except ExemptionsError as exc:
        print(f"{RED}{BOLD}exemptions unreadable{RESET}: {exc}")
        return 2
    # Several findings can share one key (the same statement twice in a file),
    # so each uncovered occurrence takes its own finding rather than the last.
    by_key: dict[tuple[str, str], list] = {}
    for f in findings:
        by_key.setdefault((f.path, line_key(f.statement)), []).append(f)
    uncovered, stale = match([(f.path, line_key(f.statement)) for f in findings], entries)
    for key in uncovered:
        f = by_key[key].pop()
        print(f"{RED}outside-Workspace write, not exempt{RESET}: {CYAN}{f.path}:{f.line}{RESET} "
              f"{BOLD}{f.kind}{RESET}  {GRAY}{f.statement.strip()[:160]}{RESET}")
    for e in stale:
        print(f"{RED}stale exemption{RESET}: {CYAN}{e.path}{RESET} {GRAY}{e.statement[:160]}{RESET}")
    if uncovered or stale:
        print(f"{GRAY}Give the temp file a Workspace directory "
              f"(`dir=data_write_dir(\"runtime:<service>/tmp\")`), or, where the "
              f"statement only COMPARES a path, record why in {EXEMPTIONS_REL}.{RESET}")
    print(f"{BOLD}searched{RESET}: {n_files} files  {BOLD}findings{RESET}: {len(findings)}  "
          f"{BOLD}exempt{RESET}: {len(findings) - len(uncovered)}  "
          f"{BOLD}not exempt{RESET}: {len(uncovered)}  {BOLD}stale{RESET}: {len(stale)}")
    return 1 if (uncovered or stale) else 0


if __name__ == "__main__":
    sys.exit(main())
