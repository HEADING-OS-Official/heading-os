#!/usr/bin/env python3
"""Gate: every outbound HTTP call in the engine sends the one workspace User-Agent.

Usage:
    python scripts/check-user-agent.py            # report, exit 1 on any finding
    python scripts/check-user-agent.py --json

The rule it holds is the operator's, 2026-09-27: the User-Agent is one constant
for the whole workspace, supplied everywhere the workspace talks to the outside
world, so that changing it in one place changes it everywhere. The constant is
`config/user-agent.txt`, read by `scripts/utils/user_agent.py`.

WHAT IT REFUSES, in tracked engine files outside `tests/`:

1. A LITERAL User-Agent in Python: a dict entry keyed `User-Agent` (any case),
   a `x["User-Agent"] = ...` assignment, a `user_agent=` keyword, or an
   exchangelib `USERAGENT` assignment, whose value is a string literal. A
   literal is a second copy of the value, which is the thing the rule forbids.
2. An OUTBOUND CALL SITE whose enclosing function never reads the constant.
   The sites are the clients this engine uses: urllib (`Request`, `urlopen`),
   requests and httpx, aiohttp sessions, Playwright contexts and pages, the
   Anthropic and OpenAI SDK clients, yt-dlp, youtube-transcript-api, Apify, and
   an exchangelib `Configuration`, and the Google clients (`googleapiclient`
   `build`, the google-auth `Request`, OAuth flows). "Reads the constant" means the function names
   `user_agent`, `USER_AGENT`, `USERAGENT` or one of the named wrappers
   (`_user_agent`, `workspace_user_agent`, `_workspace_user_agent`), or calls a
   function in the same file that does, or uses a name imported from a
   `*user_agent` module (`scripts/utils/google_user_agent.py`). A parameter of that name does not
   count: it carries whatever the caller passed. It is a
   function-level test, not a data-flow proof: a function
   that reads the constant and then sends a request without it passes. That
   limit is stated here rather than left to be discovered.
3. A `curl` or `wget` command line in a tracked shell script, markdown file or
   git hook that does not name `user-agent` / `user_agent`.

Anything that must not carry the constant is listed, with its reason, in
`config/user-agent-exemptions.json` (loopback and local-model calls, the
operator's real Brave, bootstrap commands that run before the clone exists). An
exemption that matches no site is itself a finding, so the list cannot rot.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXEMPTIONS = ROOT / "config" / "user-agent-exemptions.json"

# Exact names only. A suffix match let `browser_user_agent`, which returns the
# launched browser's own string, pass as a reader of the constant.
READS_CONSTANT = re.compile(
    r"^(user_agent|USER_AGENT|USERAGENT|_user_agent|workspace_user_agent|_workspace_user_agent)$")
UA_KEY = re.compile(r"^user[-_]?agent$", re.I)
SHELL_CMD = re.compile(r"(^|[\s`(|;&$])(curl|wget)\s")
SHELL_UA = re.compile(r"user[-_]agent", re.I)

# Outbound call names. A bare `Request` / `Configuration` counts only when the
# file imports it from the client that makes it outbound: google-auth also has a
# `Request`, and it is a different thing.
ATTR_CALLS = {
    "urlopen", "new_context", "new_page", "launch_persistent_context",
    "YoutubeDL", "YouTubeTranscriptApi", "ApifyClient",
    "Anthropic", "AsyncAnthropic", "OpenAI", "AsyncOpenAI", "ClientSession",
    "from_client_secrets_file", "from_client_config",
}
MODULE_CALLS = {
    ("request", "Request"),
    ("requests", "get"), ("requests", "post"), ("requests", "put"), ("requests", "patch"),
    ("requests", "delete"), ("requests", "head"), ("requests", "request"), ("requests", "Session"),
    ("httpx", "get"), ("httpx", "post"), ("httpx", "Client"), ("httpx", "AsyncClient"),
}
IMPORTED_NAME_CALLS = {
    "Request": ("urllib.request", "google.auth.transport.requests"),
    "Configuration": ("exchangelib",),
    "build": ("googleapiclient",),
}


@dataclass
class Finding:
    path: str
    line: int
    function: str
    kind: str
    detail: str


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files"], check=True,
                         capture_output=True, text=True).stdout
    return [p for p in out.splitlines() if not p.startswith("tests/")]


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        head = _dotted(node.value)
        return f"{head}.{node.attr}" if head else node.attr
    return ""


def _is_str_literal(node: ast.AST | None) -> bool:
    return isinstance(node, ast.JoinedStr) or (
        isinstance(node, ast.Constant) and isinstance(node.value, str))


def _imported_from(tree: ast.Module) -> dict[str, str]:
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                names[alias.asname or alias.name] = node.module
    return names


def _names(scope: ast.AST) -> set[str]:
    """Names a scope uses, minus its own parameters.

    A parameter called `user_agent` carries whatever the caller passed, which is
    not evidence that the constant was read; counting it let a function that
    forwards a browser's own string pass, and pass on to every caller.
    """
    used = {n.id if isinstance(n, ast.Name) else n.attr
            for n in ast.walk(scope) if isinstance(n, (ast.Name, ast.Attribute))}
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        a = scope.args
        used -= {x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs)}
    return used


def _readers(tree: ast.Module) -> set[str]:
    """Functions in this file that read the constant, directly or through each other.

    So a site whose headers come from a same-file helper (`get_headers()`)
    counts as reading it. Transitive within the file only; a helper imported
    from another module is not followed.
    """
    funcs = {n.name: _names(n) for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    # Anything imported from a `*user_agent` module (the constant's own module,
    # `google_user_agent`) is a reader by construction.
    imported_readers = {alias.asname or alias.name for n in ast.walk(tree)
                        if isinstance(n, ast.ImportFrom) and (n.module or "").endswith("user_agent")
                        for alias in n.names}
    readers = imported_readers | {name for name, used in funcs.items()
                                  if any(READS_CONSTANT.search(u) for u in used)}
    while True:
        more = {name for name, used in funcs.items() if name not in readers and used & readers}
        if not more:
            return readers
        readers |= more


def _reads_constant(scope: ast.AST, readers: set[str]) -> bool:
    used = _names(scope)
    return any(READS_CONSTANT.search(u) for u in used) or bool(used & readers)


def _is_outbound(call: ast.Call, imported: dict[str, str]) -> str | None:
    dotted = _dotted(call.func)
    if not dotted:
        return None
    parts = dotted.split(".")
    last = parts[-1]
    if len(parts) >= 2 and (parts[-2], last) in MODULE_CALLS:
        return dotted
    if last in ATTR_CALLS:
        if len(parts) == 1 and last == "urlopen" and not imported.get("urlopen", "").startswith("urllib"):
            return None
        return dotted
    if len(parts) == 1 and last in IMPORTED_NAME_CALLS:
        source = imported.get(last, "")
        if source.startswith(IMPORTED_NAME_CALLS[last]):
            return dotted
    return None


def scan_python(path: str, source: str) -> list[Finding]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [Finding(path, exc.lineno or 0, "<module>", "unparseable", str(exc))]
    imported = _imported_from(tree)
    # Computed on the first outbound site, never up front. It walks every
    # function body and was half of this gate's time, spent mostly on files
    # with no outbound call at all, where nothing ever reads it.
    readers: set[str] | None = None
    findings: list[Finding] = []

    def visit(scope: ast.AST, fname: str) -> None:
        for node in ast.iter_child_nodes(scope):
            _walk(node, scope, fname)

    def _walk(node: ast.AST, scope: ast.AST, fname: str) -> None:
        nonlocal readers
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.iter_child_nodes(node):
                _walk(child, node, node.name)
            return
        if isinstance(node, ast.ClassDef):
            for child in ast.iter_child_nodes(node):
                _walk(child, scope, fname)
            return
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if _is_str_literal(key) and isinstance(key, ast.Constant) \
                        and UA_KEY.match(key.value) and _is_str_literal(value):
                    findings.append(Finding(path, node.lineno, fname, "literal",
                                            "dict entry User-Agent set to a string literal"))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant) \
                        and isinstance(target.slice.value, str) and UA_KEY.match(target.slice.value) \
                        and _is_str_literal(node.value):
                    findings.append(Finding(path, node.lineno, fname, "literal",
                                            "User-Agent header assigned a string literal"))
                if isinstance(target, ast.Attribute) and target.attr == "USERAGENT" \
                        and _is_str_literal(node.value):
                    findings.append(Finding(path, node.lineno, fname, "literal",
                                            "USERAGENT assigned a string literal"))
        elif isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg and UA_KEY.match(kw.arg) and _is_str_literal(kw.value):
                    findings.append(Finding(path, node.lineno, fname, "literal",
                                            f"{kw.arg}= keyword set to a string literal"))
            site = _is_outbound(node, imported)
            if site and readers is None:
                readers = _readers(tree)
            if site and not _reads_constant(scope, readers):
                findings.append(Finding(path, node.lineno, fname, "site",
                                        f"{site}(...) in a scope that never reads the workspace User-Agent"))
        for child in ast.iter_child_nodes(node):
            _walk(child, scope, fname)

    visit(tree, "<module>")
    return findings


def scan_text(path: str, text: str) -> list[Finding]:
    return [Finding(path, n, "<line>", "shell", line.strip()[:120])
            for n, line in enumerate(text.splitlines(), 1)
            if SHELL_CMD.search(line) and not SHELL_UA.search(line)
            and not line.lstrip().startswith("#")]


def load_exemptions() -> list[dict]:
    data = json.loads(EXEMPTIONS.read_text(encoding="utf-8"))
    entries = data["exemptions"]
    for entry in entries:
        missing = {"path", "function", "reason"} - set(entry)
        if missing or not entry["reason"].strip():
            raise SystemExit(f"{EXEMPTIONS}: entry {entry} lacks {sorted(missing) or 'a reason'}")
    return entries


def run(files: list[str] | None = None) -> list[Finding]:
    findings: list[Finding] = []
    for rel in files if files is not None else tracked_files():
        path = ROOT / rel
        if not path.is_file():
            continue
        is_py = rel.endswith(".py") and rel.startswith(("scripts/", ".claude/"))
        is_text = rel.endswith((".sh", ".bash", ".md")) or rel.startswith(".githooks/")
        if not (is_py or is_text):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            findings.append(Finding(rel, 0, "<file>", "unreadable", str(exc)))
            continue
        findings.extend(scan_python(rel, text) if is_py else scan_text(rel, text))

    exemptions = load_exemptions()
    used: set[int] = set()
    kept: list[Finding] = []
    for f in findings:
        hit = next((i for i, e in enumerate(exemptions)
                    if e["path"] == f.path and f.kind in ("site", "shell")
                    and (e["function"] == f.function
                         or (f.kind == "shell" and e["function"] in f.detail))), None)
        if hit is None:
            kept.append(f)
        else:
            used.add(hit)
    if files is None:
        for i, e in enumerate(exemptions):
            if i not in used:
                kept.append(Finding(e["path"], 0, e["function"], "stale-exemption",
                                    "exemption matches no outbound site; remove it"))
    return kept


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("files", nargs="*", help="scan only these repo-relative files")
    args = parser.parse_args(argv)
    findings = run(args.files or None)
    if args.json:
        print(json.dumps([asdict(f) for f in findings], indent=2))
    else:
        for f in findings:
            print(f"{f.path}:{f.line}: [{f.kind}] {f.function}: {f.detail}")
        print(f"user-agent gate: {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
