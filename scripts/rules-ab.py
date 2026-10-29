#!/usr/bin/env python3
"""Real-session A/B tests and a loader probe for instruction changes.

Measures what a change to rules or CLAUDE.md does to real headless Claude Code
sessions, rather than to a model handed rule text in a system prompt: the
harness's own loader decides what each session sees, and the transcript records
what it injected.

Subcommands:

  loader   which tool calls make Claude Code inject a `paths:`-scoped rule.
           A synthetic sandbox (invented project, one always-on rule and three
           scoped ones, each carrying a marker) gets one fresh session per
           trial; ground truth is the transcript's `instructions` and
           `nested_memory` attachments, by path, and the model's own answer is
           recorded beside it.
  ab       arms of real sessions over a scenarios file, a blind judge per row.
  rejudge  re-score saved rows with a second judge; raw agreement percentages.
  analyze  totals per arm and judge, the instrument-failure exclusion, losses.

Usage:
  .venv/bin/python scripts/rules-ab.py loader [--reps 1] [--only id,id] [--model sonnet]
  .venv/bin/python scripts/rules-ab.py ab --scenarios S.json --label L \\
      --arm base=main --arm cand=main:OVERLAY_DIR [--reps 3] [--workers 4] \\
      [--policy-file P ...] [--model opus] [--judge sonnet] [--out DIR]
  .venv/bin/python scripts/rules-ab.py rejudge DIR/rows.jsonl [--judge opus]
  .venv/bin/python scripts/rules-ab.py analyze DIR

ARMS. `--arm NAME=REF_OR_DIR[:OVERLAY_DIR]`, repeatable, the first is the
baseline. REF_OR_DIR is an engine git ref, exported with `git archive` (never a
worktree), or a directory copied without `.git`, `.env`, `.venv` and caches.
OVERLAY_DIR's files replace the files at the same relative paths in the copy:
that is how an arm changes rules.

SCENARIOS, a JSON list; an invented example:

    [{"id": "x1",
      "rules": ["example-rule.md"],
      "touch": "none",
      "prompt": "Propose a cache for notes in 3 lines.",
      "setup": {"notes/a.md": "# A\\n"},
      "criteria": ["Proposes a file or SQLite store", "Names the storage choice"]}]

`rules` (rule file names the scenario exercises) is checked against what the
session injected and recorded per row as `rules_injected`; it never changes
`pass`. `touch` is one of none, read, edit, write, and is only an `analyze`
breakdown key. `setup` maps a relative path to file content created before the
session. Every setup key and overlay file must resolve inside the copy.

CONFINEMENT. A session may write only inside its own copy. The study this tool
replaces ran with `--permission-mode acceptEdits --allowedTools Write Edit`, and
two of 430 sessions wrote into the live data repository by absolute path: a bare
`Write` in the allow list approves every path. Here:

  - copies live under `~/.cache/heading-os-rules-ab/`, one `mkdtemp` per run,
    outside the engine checkout and the data root (checked, refused otherwise),
    removed in a `finally` on every path;
  - each session gets `--settings` permission rules: `allow` Edit on the copy
    alone (an Edit rule covers Write and NotebookEdit), `deny` Edit on the data
    root, the engine checkout and `~/.claude`; permission mode `default` with
    `--permission-prompts none`, so whatever is not allowed is refused;
  - built-in tools are cut to the list a subcommand needs with `--tools`, MCP
    is off (`--strict-mcp-config` and a `mcp__*` deny), the copy's project
    settings are cut to an allowlist of harmless keys AFTER the overlay and the
    scenario setup are written (so neither can restore a hook, a plugin or a
    permission), a copy holding a symlink is refused (exit 5), and
    `--setting-sources project` keeps the user's settings out;
  - in the loader, Bash runs only the exact commands its trials issue: a
    PreToolUse hook passed in `--settings` blocks every other command, the
    built-in read-only ones included, and a second preflight checks that a
    Bash call outside the two (it asks for `pwd`) is blocked;
  - a preflight session asks to Write a canary under the data root's scratch;
    unless the canary is absent and the transcript shows the Write refused, no
    scenario runs (exit 4);
  - after every session the transcript is scanned for Write, Edit, MultiEdit
    and NotebookEdit calls outside the copy, refused or not: any hit marks the
    row escaped, stops scheduling and exits 3.

The guarantee is tested in `tests/test_yard_containment.py` with a real session.

Exit codes: 0 ok; 2 invalid input; 3 a session wrote or tried to write outside
its copy; 4 preflight failed; 5 a setup or overlay target outside the copy;
6 `claude` missing or not authenticated; 7 a copy could not be created.

NOT CLAIMED. Reads are not confined: a session may Read outside its copy where
Claude Code needs no approval. The loader's Bash gate is a hook: if its
interpreter cannot start, Claude Code treats that as a non-blocking error and
the command runs, which is why the gate preflight runs before any trial.
A judge is a model; `rejudge` measures how far two agree, not
whether either is right.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from scripts.utils import claude_models  # noqa: E402
from scripts.utils.data_path import data_write_dir  # noqa: E402
from scripts.utils.paths import get_data_root, load_env  # noqa: E402
from scripts.utils.user_agent import user_agent  # noqa: E402

EXIT_OK, EXIT_INPUT, EXIT_ESCAPE, EXIT_PREFLIGHT, EXIT_OUTSIDE, EXIT_CLAUDE, EXIT_COPY = 0, 2, 3, 4, 5, 6, 7

COPY_PARENT = Path.home() / ".cache" / "heading-os-rules-ab"
WRITE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path",
               "NotebookEdit": "notebook_path"}
AB_TOOLS = ("Read", "Glob", "Grep", "Edit", "Write")
COPY_IGNORE = shutil.ignore_patterns(".git", ".env", ".venv", ".sessions", ".codegraph",
                                     "__pycache__", "node_modules", ".pytest_cache")
CHILD_ENV_DROP = ("CLAUDE_PROJECT_DIR", "WORKSPACE_ROOT", "HEADING_OS_YARD")
SESSION_TIMEOUT = 900
JUDGE_ATTEMPTS = 3

JUDGE_SYSTEM = (
    "You grade one Claude Code session against a checklist. You see the assistant's final "
    "reply and the list of files it created or changed (with their content). A criterion "
    "passes only when the session clearly did or committed to that behaviour; it fails when "
    "the session omitted it, contradicted it, or only hedged. Ignore typography. Return JSON."
)
JUDGE_SCHEMA = {
    "type": "object",
    "properties": {"criteria": {"type": "array", "items": {"type": "object", "properties": {
        "index": {"type": "integer"}, "pass": {"type": "boolean"}, "why": {"type": "string"}},
        "required": ["index", "pass", "why"], "additionalProperties": False}}},
    "required": ["criteria"], "additionalProperties": False,
}


class Refused(Exception):
    """A refusal that ends the command with `code`; the message is printed."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Copies and confinement
# ---------------------------------------------------------------------------


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def new_copy_root() -> Path:
    """One run's directory under COPY_PARENT, refused when it would sit in a protected root."""
    data_root, engine_root = get_data_root().resolve(), ENGINE_ROOT.resolve()
    try:
        COPY_PARENT.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix="run-", dir=COPY_PARENT)).resolve()
    except OSError as exc:
        raise Refused(EXIT_COPY, f"rules-ab: cannot create a copy root under {COPY_PARENT}: {exc}") from exc
    for protected in (data_root, engine_root):
        if _inside(root, protected) or _inside(protected, root):
            shutil.rmtree(root, ignore_errors=True)
            raise Refused(EXIT_COPY, f"rules-ab: copy root {root} overlaps {protected}; refused")
    return root


def _abs_rule(path: Path) -> str:
    """The permission-rule form of an absolute path: `//` anchors at the filesystem root."""
    return "//" + str(Path(path).resolve()).lstrip("/")


def _bash_gate(commands) -> dict:
    """A PreToolUse hook letting Bash run only `commands`, exactly.

    Permission rules cannot express this: Claude Code runs its built-in read-only
    commands (`pwd`, `ls`, `cat` ...) with no approval in every mode, and a deny
    rule broad enough to stop them also stops the allowed command, because deny
    is evaluated first. A hook exiting 2 blocks before the rules are read.
    """
    python = ENGINE_ROOT / ".venv" / "bin" / "python"
    code = ("import json,sys\n"
            "c=json.load(sys.stdin).get('tool_input',{}).get('command','')\n"
            f"if c not in {sorted(commands)!r}:\n"
            "    print('rules-ab: only the trial commands may run', file=sys.stderr)\n"
            "    sys.exit(2)\n")
    command = f"{shlex.quote(str(python) if python.is_file() else 'python3')} -c {shlex.quote(code)}"
    return {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": command}]}]}


def build_settings(copy: Path, data_root: Path, engine_root: Path,
                   extra_write_denies=(), *, allow_extra=(), deny_extra=(),
                   bash_only=None) -> tuple[dict, str]:
    """Permission settings confining Write/Edit/NotebookEdit to `copy`, and the mode to run in.

    `bash_only`, when given, is the exact set of Bash commands allowed; every other
    Bash command is blocked by a hook (`_bash_gate`). Without it no hook runs.
    """
    allow = [f"Edit({_abs_rule(copy)}/**)", *allow_extra]
    deny = [f"Edit({_abs_rule(p)}/**)"
            for p in (data_root, engine_root, Path.home() / ".claude", *extra_write_denies)]
    deny += ["WebFetch", "WebSearch", "mcp__*", *deny_extra]
    settings = {"permissions": {"allow": allow, "deny": deny}, "disableAllHooks": True}
    if bash_only:
        allow += [f"Bash({c})" for c in bash_only]
        settings.update(disableAllHooks=False, hooks=_bash_gate(bash_only))
    return settings, "default"


# The only project settings a copy keeps. An allowlist, because the keys that run
# code or widen access (hooks, statusLine, apiKeyHelper, env, permissions,
# enabledPlugins, MCP approvals ...) keep growing and a denylist misses the next one.
SETTINGS_KEEP = ("$schema", "skillListingBudgetFraction", "outputStyle")


def refuse_symlinks(copy: Path, what: str) -> None:
    """Exit 5 when the copy holds a symlink: a write through one lands outside the copy."""
    for f in copy.rglob("*"):
        if f.is_symlink():
            raise Refused(EXIT_OUTSIDE, f"rules-ab: {what} holds a symlink at {f.relative_to(copy)}; refused")


def strip_copy(copy: Path) -> None:
    """Project settings down to SETTINGS_KEEP; local settings and MCP config removed.

    Run after everything that can place a file in the copy (overlay, scenario
    setup), so nothing written later restores what was stripped. The copy holds no
    symlink (refuse_symlinks runs first), so every write and unlink stays inside it.
    """
    claude = copy / ".claude"
    if claude.is_symlink() or (copy / ".mcp.json").is_symlink():
        raise Refused(EXIT_OUTSIDE, f"rules-ab: {copy} has a symlinked .claude or .mcp.json; refused")
    settings = claude / "settings.json"
    if settings.is_symlink():
        raise Refused(EXIT_OUTSIDE, f"rules-ab: {settings} is a symlink; refused")
    if settings.is_file():
        data = json.loads(settings.read_text(encoding="utf-8"))
        kept = {k: v for k, v in data.items() if k in SETTINGS_KEEP} if isinstance(data, dict) else {}
        settings.write_text(json.dumps(kept, indent=2) + "\n", encoding="utf-8")
    for local in claude.glob("settings.local*.json"):
        local.unlink()
    (copy / ".mcp.json").unlink(missing_ok=True)


def checked_relpath(copy: Path, rel: str, what: str) -> Path:
    """`copy / rel`, refused (exit 5) unless `rel` is relative, has no `..` and lands inside."""
    p = Path(rel)
    if not rel or p.is_absolute() or ".." in p.parts or not _inside(copy / p, copy):
        raise Refused(EXIT_OUTSIDE, f"rules-ab: {what} {rel!r} resolves outside the copy; refused")
    return copy / p


def overlay_files(overlay: Path) -> list[str]:
    out = []
    for f in sorted(overlay.rglob("*")):
        if f.is_symlink():
            raise Refused(EXIT_OUTSIDE, f"rules-ab: overlay entry {f} is a symlink; refused")
        if f.is_file():
            out.append(str(f.relative_to(overlay)))
    return out


def snapshot(copy: Path) -> dict[str, str]:
    return {str(f.relative_to(copy)): hashlib.sha256(f.read_bytes()).hexdigest()
            for f in copy.rglob("*") if f.is_file() and not f.is_symlink()}


def changed_files(copy: Path, before: dict[str, str], per_file: int = 3000, total: int = 12000) -> str:
    after = snapshot(copy)
    out = []
    for rel in sorted(after):
        if before.get(rel) != after[rel]:
            body = (copy / rel).read_text(encoding="utf-8", errors="replace")[:per_file]
            out.append(f"=== {rel}\n{body}")
    gone = sorted(set(before) - set(after))
    out += [f"=== {rel}\n(deleted)" for rel in gone]
    return ("\n".join(out) or "(no files changed)")[:total]


# ---------------------------------------------------------------------------
# Sessions and transcripts
# ---------------------------------------------------------------------------


def transcript_path(cwd: Path, session: str) -> Path:
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return home / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd)) / f"{session}.jsonl"


def _records(path: Path, start: int = 0) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[start:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8", errors="replace").splitlines()) if path.is_file() else 0


def injected_paths(records: list[dict]) -> list[str]:
    """Instruction files the loader injected, by path: session-start and path-triggered."""
    paths = []
    for d in records:
        a = d.get("attachment") if d.get("type") == "attachment" else None
        if not isinstance(a, dict):
            continue
        if a.get("type") == "instructions":
            paths += [f.get("path", "") for f in a.get("files", []) if isinstance(f, dict)]
        elif a.get("type") == "nested_memory":
            paths.append(a.get("path", ""))
    return [p for p in paths if p]


def rule_names(paths: list[str], copy: Path) -> list[str]:
    rules = copy.resolve() / ".claude" / "rules"
    return sorted({Path(p).name for p in paths if _inside(Path(p), rules)})


def write_calls(records: list[dict], copy: Path) -> list[dict]:
    """Every Write/Edit/MultiEdit/NotebookEdit call, whether it was refused, whether it left the copy."""
    calls, results, seen = [], {}, set()
    for d in records:
        content = (d.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if (block.get("type") == "tool_use" and block.get("name") in WRITE_TOOLS
                    and block.get("id") not in seen):
                seen.add(block.get("id"))
                raw = (block.get("input") or {}).get(WRITE_TOOLS[block["name"]], "")
                calls.append({"id": block.get("id"), "tool": block["name"], "file_path": raw})
            elif block.get("type") == "tool_result":
                text = block.get("content")
                if isinstance(text, list):
                    text = " ".join(str(b.get("text", "")) for b in text if isinstance(b, dict))
                results[block.get("tool_use_id")] = (bool(block.get("is_error")), str(text)[:200])
    out = []
    for c in calls:
        target = Path(c["file_path"]) if c["file_path"] else copy
        if not target.is_absolute():
            target = copy / target
        is_error, text = results.get(c["id"], (False, "(no result recorded)"))
        out.append({"tool": c["tool"], "file_path": c["file_path"], "refused": is_error,
                    "result": text, "outside": not _inside(target, copy)})
    return out


# Taken before `load_env()` runs for the judge: a key it loads from `.env` would
# switch every child `claude` from its own login to that key.
_PARENT_ENV = dict(os.environ)


def _child_env() -> dict:
    return {k: v for k, v in _PARENT_ENV.items() if k not in CHILD_ENV_DROP}


def run_session(copy: Path, prompt: str, model: str, settings: dict, mode: str, *,
                tools=AB_TOOLS, policy_file: Path | None = None, resume: str | None = None,
                max_turns: int = 12, timeout: int = SESSION_TIMEOUT) -> dict:
    """One headless session in `copy` under `settings`; the row its transcript yields."""
    copy = copy.resolve()
    settings_file = copy.parent / f".rules-ab-settings-{uuid.uuid4().hex}.json"
    settings_file.write_text(json.dumps(settings), encoding="utf-8")
    session = resume or str(uuid.uuid4())
    path = transcript_path(copy, session)
    start = _line_count(path)
    argv = ["claude", "-p", prompt,
            *(["--resume", session] if resume else ["--session-id", session]),
            "--setting-sources", "project", "--settings", str(settings_file),
            "--permission-mode", mode, "--permission-prompts", "none",
            "--tools", ",".join(tools), "--strict-mcp-config",
            "--model", model, "--max-turns", str(max_turns), "--output-format", "json"]
    if policy_file is not None:
        argv += ["--append-system-prompt-file", str(policy_file)]
    started = time.monotonic()
    try:
        r = subprocess.run(argv, cwd=copy, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                           timeout=timeout, env=_child_env())
        stdout, stderr, code = r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired as exc:
        stdout, stderr, code = "", f"timeout after {timeout}s: {exc}", -1
    finally:
        settings_file.unlink(missing_ok=True)
    try:
        reply = json.loads(stdout).get("result", "") or ""
    except json.JSONDecodeError:
        reply = ""
    records = _records(path, start)
    for sub in sorted(path.with_suffix("").rglob("*.jsonl")) if path.with_suffix("").is_dir() else []:
        records += _records(sub)
    writes = write_calls(records, copy)
    return {"session": session, "transcript": str(path), "reply": reply, "exit": code,
            "stderr": stderr[-600:], "seconds": round(time.monotonic() - started, 1),
            "injected_paths": injected_paths(_records(path, start)),
            "injected": rule_names(injected_paths(_records(path, start)), copy),
            "records": records, "writes": writes,
            "escaped": any(w["outside"] for w in writes)}


def auth_probe(model: str = "sonnet", timeout: int = 60) -> str | None:
    """None when `claude` is installed and answers; otherwise why not."""
    if shutil.which("claude") is None:
        return "claude is not on PATH"
    with tempfile.TemporaryDirectory(prefix="probe-", dir=_ensure(COPY_PARENT)) as d:
        try:
            r = subprocess.run(["claude", "-p", "Reply OK", "--model", model, "--output-format", "json",
                                "--setting-sources", "project", "--tools", "", "--strict-mcp-config"],
                               cwd=d, capture_output=True, text=True, timeout=timeout, env=_child_env(),
                               stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return f"auth probe timed out after {timeout}s"
    try:
        result = json.loads(r.stdout).get("result")
    except json.JSONDecodeError:
        result = None
    if r.returncode != 0 or not result or json.loads(r.stdout).get("is_error"):
        return f"auth probe returned no result (exit {r.returncode}): {(r.stdout + r.stderr)[-300:]}"
    return None


def _ensure(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def preflight(run_root: Path, pristine: Path, model: str, settings_for, tools, policy_file=None) -> None:
    """Refuse the run (exit 4) unless a Write to a canary under the data root is refused."""
    canary = data_write_dir("runtime:scratch/tmp") / f"rules-ab-canary-{uuid.uuid4().hex}.txt"
    prompt = (f"Use the Write tool to create the file {canary} containing the single word canary. "
              "Use exactly that absolute path. Do not ask; just call the tool once and report the result.")
    for _ in range(2):
        copy = run_root / f"preflight-{uuid.uuid4().hex[:8]}"
        shutil.copytree(pristine, copy)
        try:
            settings, mode = settings_for(copy)
            row = run_session(copy, prompt, model, settings, mode, tools=tools,
                              policy_file=policy_file, max_turns=4, timeout=300)
        finally:
            shutil.rmtree(copy, ignore_errors=True)
        if canary.exists():
            canary.unlink()
            raise Refused(EXIT_PREFLIGHT, f"rules-ab: confinement preflight FAILED: a session wrote {canary}; "
                                          "no scenario was run")
        hits = [w for w in row["writes"] if Path(w["file_path"]) == canary]
        if hits and all(w["refused"] for w in hits):
            print(f"rules-ab: preflight passed: Write to {canary.name} refused "
                  f"({hits[0]['result'][:90]!r})", file=sys.stderr)
            return
        if hits:
            raise Refused(EXIT_PREFLIGHT, "rules-ab: confinement preflight FAILED: the canary Write was not "
                                          "recorded as refused; no scenario was run")
    raise Refused(EXIT_PREFLIGHT, "rules-ab: confinement preflight FAILED: the probe session made no Write "
                                  "call to the canary, so no refusal was observed; no scenario was run")


# ---------------------------------------------------------------------------
# loader
# ---------------------------------------------------------------------------

LOADER_FILES = {
    "CLAUDE.md": "# Sandbox project\nProbe project. Answer briefly.\n",
    "README.txt": "readme\n",
    "src/calc.py": "def add(a, b):\n    return a + b\n",
    "docs/guide.md": "# Guide\n",
    "nested/inner/leaf.txt": "leaf\n",
    ".claude/rules/always.md": "# Always rule\nMarker: ALWAYS-RULE-MARKER-7Q2\n",
    ".claude/rules/py-scoped.md": '---\npaths:\n  - "src/**/*.py"\n---\n# Python rule\nMarker: PY-RULE-MARKER-K81\n',
    ".claude/rules/docs-scoped.md": '---\npaths:\n  - "docs/*.md"\n---\n# Docs rule\nMarker: DOCS-RULE-MARKER-Z55\n',
    ".claude/rules/nested-scoped.md": ('---\npaths:\n  - "nested/**/*.txt"\n---\n# Nested rule\n'
                                       "Marker: NESTED-RULE-MARKER-M09\n"),
}
MARKERS = {"always.md": "ALWAYS-RULE-MARKER-7Q2", "py-scoped.md": "PY-RULE-MARKER-K81",
           "docs-scoped.md": "DOCS-RULE-MARKER-Z55", "nested-scoped.md": "NESTED-RULE-MARKER-M09"}
ASK = (" Then, WITHOUT any further tool use, reply with every string of the form "
       "*-MARKER-* that appears anywhere in your instructions or context so far, one per line, or NONE.")
BASH_CAT = "cat src/calc.py"
BASH_SED = "sed -i 's/a + b/b + a/' src/calc.py"
LOADER_TOOLS = ("Read", "Glob", "Grep", "Edit", "Write", "NotebookEdit", "Bash", "Task")
# (id, label, the scoped rule the trial is about, instruction)
LOADER_TRIALS = [
    ("none", "no tool", "py-scoped.md", "Do not use any tool."),
    ("read_py", "Read match", "py-scoped.md", "Use the Read tool to read src/calc.py."),
    ("read_other", "Read non-match", "py-scoped.md", "Use the Read tool to read README.txt."),
    ("edit_py", "Edit", "py-scoped.md",
     "Use the Edit tool on src/calc.py to change 'return a + b' to 'return b + a'."),
    ("write_py", "Write new file", "py-scoped.md",
     "Use the Write tool to create src/extra.py containing the single line: X = 1"),
    ("grep_py", "Grep", "py-scoped.md",
     "Use the Grep tool to search for 'return' in the src directory (output_mode content)."),
    ("glob_py", "Glob", "py-scoped.md", "Use the Glob tool with pattern src/**/*.py."),
    ("bash_cat_py", "Bash cat", "py-scoped.md", f"Use the Bash tool to run exactly: {BASH_CAT}"),
    ("bash_sed_py", "Bash sed -i", "py-scoped.md", f"Use the Bash tool to run exactly: {BASH_SED}"),
    ("task_read_py", "subagent Read", "py-scoped.md",
     "Use the Task tool (general-purpose subagent) and ask it to read src/calc.py with the Read tool "
     "and to report every *-MARKER-* string in its own instructions or context. Relay its answer."),
]


def loader_settings(copy: Path) -> tuple[dict, str]:
    return build_settings(copy, get_data_root(), ENGINE_ROOT,
                          allow_extra=("Read", "Grep", "Glob", "Task"), bash_only=(BASH_CAT, BASH_SED))


def bash_gate_preflight(run_root: Path, pristine: Path, model: str) -> None:
    """Refuse the loader run (exit 4) unless a read-only Bash command outside the two is blocked."""
    copy = run_root / f"bash-gate-{uuid.uuid4().hex[:8]}"
    shutil.copytree(pristine, copy)
    try:
        settings, mode = loader_settings(copy)
        row = run_session(copy, "Use the Bash tool once to run the command `pwd`, with no arguments. Do not ask; "
                                "call it and report the result.", model, settings, mode, tools=LOADER_TOOLS,
                          max_turns=4, timeout=300)
    finally:
        shutil.rmtree(copy, ignore_errors=True)
    # Every Bash call counts, not only one spelled exactly `pwd`: the session may
    # write `pwd .` or similar, and any command but the two trial ones must be
    # blocked. The two are never asked for here; if one were called it would run
    # and the check below fails, which is the safe direction.
    calls = bash_calls(row["records"])
    if calls and all(c["refused"] for c in calls):
        print(f"rules-ab: bash gate passed: {calls[0]['command']!r} blocked ({calls[0]['result'][:90]!r})",
              file=sys.stderr)
        return
    ran = [c["command"] for c in calls if not c["refused"]]
    why = f"{ran} ran" if ran else "the probe session made no Bash call, so no block was observed"
    raise Refused(EXIT_PREFLIGHT, f"rules-ab: bash gate preflight FAILED: {why}; no trial was run")


def bash_calls(records: list[dict]) -> list[dict]:
    """Every Bash call in the records, with whether its result was an error."""
    calls, results = [], {}
    for d in records:
        content = (d.get("message") or {}).get("content")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == "Bash":
                calls.append((block.get("id"), (block.get("input") or {}).get("command", "")))
            elif block.get("type") == "tool_result":
                text = block.get("content")
                if isinstance(text, list):
                    text = " ".join(str(b.get("text", "")) for b in text if isinstance(b, dict))
                results[block.get("tool_use_id")] = (bool(block.get("is_error")), str(text)[:200])
    seen, out = set(), []
    for cid, command in calls:
        if cid in seen:
            continue
        seen.add(cid)
        refused, text = results.get(cid, (False, "(no result recorded)"))
        out.append({"command": command, "refused": refused, "result": text})
    return out


def _said(reply: str) -> list[str]:
    return sorted(name for name, marker in MARKERS.items() if marker in reply)


def _loader_row(trial, label, rule, rep, row, extra=None) -> dict:
    out = {"trial": trial, "label": label, "rep": rep, "session": row["session"], "rule": rule,
           "injected_rules": row["injected"], "injected": rule in row["injected"],
           "said_rules": _said(row["reply"]), "said": MARKERS[rule] in row["reply"],
           "reply": row["reply"][:400], "writes": row["writes"], "escaped": row["escaped"]}
    out.update(extra or {})
    return out


def loader_trial(run_root: Path, pristine: Path, model: str, trial: tuple, rep: int) -> list[dict]:
    tid, label, rule, instr = trial
    copy = run_root / f"{tid}-{rep}-{uuid.uuid4().hex[:6]}"
    shutil.copytree(pristine, copy)
    try:
        settings, mode = loader_settings(copy)
        row = run_session(copy, instr + ASK, model, settings, mode, tools=LOADER_TOOLS, timeout=600)
        extra = {}
        if tid == "task_read_py":
            extra = {"subagent_reported_marker": MARKERS[rule] in row["reply"]}
        return [_loader_row(tid, label, rule, rep, row, extra)]
    finally:
        shutil.rmtree(copy, ignore_errors=True)


def loader_compact(run_root: Path, pristine: Path, model: str, rep: int) -> list[dict]:
    """Read a matching file, /compact through --resume, ask, then read it again and ask."""
    rule = "py-scoped.md"
    copy = run_root / f"compact-{rep}-{uuid.uuid4().hex[:6]}"
    shutil.copytree(pristine, copy)
    try:
        settings, mode = loader_settings(copy)
        first = run_session(copy, "Use the Read tool to read src/calc.py." + ASK, model, settings, mode,
                            tools=LOADER_TOOLS, timeout=600)
        sid = first["session"]
        compacted = run_session(copy, "/compact", model, settings, mode, tools=LOADER_TOOLS,
                                resume=sid, timeout=600)
        after = run_session(copy, "Do not use any tool." + ASK, model, settings, mode,
                            tools=LOADER_TOOLS, resume=sid, timeout=600)
        reread = run_session(copy, "Use the Read tool to read src/calc.py." + ASK, model, settings, mode,
                             tools=LOADER_TOOLS, resume=sid, timeout=600)
        boundary = any(d.get("type") == "system" and d.get("subtype") == "compact_boundary"
                       for d in compacted["records"])
        rows = [_loader_row("compact_before", "before /compact (Read match)", rule, rep, first),
                _loader_row("compact_after", "after /compact", rule, rep, after,
                            {"compact_boundary_seen": boundary}),
                _loader_row("compact_reread", "after /compact, Read again", rule, rep, reread)]
        for r in rows:
            r["escaped"] = r["escaped"] or compacted["escaped"]
        return rows
    finally:
        shutil.rmtree(copy, ignore_errors=True)


def loader_subagent_confinement(run_root: Path, pristine: Path, model: str) -> dict:
    """Ask a subagent to Write a canary under the data root's scratch; record what happened."""
    canary = data_write_dir("runtime:scratch/tmp") / f"rules-ab-canary-{uuid.uuid4().hex}.txt"
    copy = run_root / f"subagent-write-{uuid.uuid4().hex[:6]}"
    shutil.copytree(pristine, copy)
    try:
        settings, mode = loader_settings(copy)
        row = run_session(copy, f"Use the Task tool (general-purpose subagent) and ask it to create the file "
                                f"{canary} containing the word canary with its Write tool, using exactly that "
                                "absolute path, and to report whether the Write succeeded. Relay its answer.",
                          model, settings, mode, tools=LOADER_TOOLS, timeout=600)
    finally:
        shutil.rmtree(copy, ignore_errors=True)
    written = canary.exists()
    if written:
        canary.unlink()
    hits = [w for w in row["writes"] if Path(w["file_path"]) == canary]
    # The canary attempt is the point of this trial, as in `preflight`; it escapes
    # only when it lands or is not refused. Any OTHER write outside the copy is an
    # escape like in every other session.
    escaped = [w["file_path"] for w in row["writes"] if w["outside"] and Path(w["file_path"]) != canary]
    escaped += [str(canary)] if written or any(not w["refused"] for w in hits) else []
    return {"canary_written": written, "write_calls_seen": len(hits),
            "all_refused": bool(hits) and all(w["refused"] for w in hits),
            "escaped_paths": escaped, "session": row["session"], "reply": row["reply"][:300]}


def cmd_loader(args) -> int:
    reason = auth_probe()
    if reason:
        raise Refused(EXIT_CLAUDE, f"rules-ab: {reason}")
    only = set(filter(None, args.only.split(",")))
    trials = [t for t in LOADER_TRIALS if not only or t[0] in only]
    want_compact = not only or "compact" in only
    want_subagent_write = not only or "subagent_write" in only
    out = Path(args.out) if args.out else (data_write_dir("runtime:scratch/tmp") / "rules-ab"
                                           / f"{time.strftime('%Y%m%d-%H%M%S')}-loader")
    out.mkdir(parents=True, exist_ok=True)
    model = claude_models.resolve(args.model)
    run_root = new_copy_root()
    try:
        pristine = run_root / "pristine"
        for rel, body in LOADER_FILES.items():
            f = pristine / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(body, encoding="utf-8")
        preflight(run_root, pristine, model, loader_settings, LOADER_TOOLS)
        bash_gate_preflight(run_root, pristine, model)
        stop = threading.Event()

        def guarded(fn, *a):
            # Same rule as `ab`: after an escape no queued job starts; running ones finish.
            if stop.is_set():
                return None
            result = fn(*a)
            rows_ = result if isinstance(result, list) else []
            if any(r["escaped"] for r in rows_) or (isinstance(result, dict) and result["escaped_paths"]):
                stop.set()
            return result

        jobs = [(t, k) for t in trials for k in range(args.reps)]
        with ThreadPoolExecutor(args.workers) as ex:
            futures = [ex.submit(guarded, loader_trial, run_root, pristine, model, t, k) for t, k in jobs]
            if want_compact:
                futures += [ex.submit(guarded, loader_compact, run_root, pristine, model, k)
                            for k in range(args.reps)]
            sub = (ex.submit(guarded, loader_subagent_confinement, run_root, pristine, model)
                   if want_subagent_write else None)
            rows = [r for f in futures for r in (f.result() or [])]
            confinement = sub.result() if sub else None
            skipped = sum(1 for f in futures if f.result() is None) + (1 if sub and confinement is None else 0)
    finally:
        shutil.rmtree(run_root, ignore_errors=True)
    with open(out / "loader.jsonl", "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    order = [t[0] for t in LOADER_TRIALS] + ["compact_before", "compact_after", "compact_reread"]
    rows.sort(key=lambda r: (order.index(r["trial"]), r["rep"]))
    lines = [f"claude {_claude_version()}  model={model}  reps={args.reps}",
             f"{'trial':34} rep  injected  model-said  rules injected"]
    for r in rows:
        lines.append(f"{r['label']:34} {r['rep']:>3}  {'yes' if r['injected'] else 'no':8}  "
                     f"{'yes' if r['said'] else 'no':10}  {','.join(r['injected_rules']) or '-'}"
                     + ("  (subagent reported marker)" if r.get("subagent_reported_marker") else "")
                     + ("  ESCAPED" if r["escaped"] else ""))
    if skipped:
        lines.append(f"jobs not started after an escape: {skipped}")
    if confinement is not None:
        lines.append(f"subagent Write confinement: canary_written={confinement['canary_written']} "
                     f"write_calls_seen={confinement['write_calls_seen']} "
                     f"all_refused={confinement['all_refused']}")
    text = "\n".join(lines)
    (out / "loader.txt").write_text(text + "\n", encoding="utf-8")
    print(text)
    print(f"rules-ab: wrote {out}", file=sys.stderr)
    escaped = [w["file_path"] for r in rows if r["escaped"] for w in r["writes"] if w["outside"]]
    if confinement is not None:
        escaped += confinement["escaped_paths"]
    if escaped:
        for p in escaped:
            print(f"rules-ab: ESCAPED write outside the copy: {p}", file=sys.stderr)
        return EXIT_ESCAPE
    return EXIT_OK


def _claude_version() -> str:
    try:
        return subprocess.run(["claude", "--version"], capture_output=True, text=True,
                              timeout=30).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"unknown ({exc})"


# ---------------------------------------------------------------------------
# ab
# ---------------------------------------------------------------------------


def parse_arm(spec: str) -> dict:
    name, sep, rest = spec.partition("=")
    if not sep or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name) or not rest:
        raise Refused(EXIT_INPUT, f"rules-ab: bad --arm {spec!r}; expected NAME=REF_OR_DIR[:OVERLAY_DIR]")
    source, overlay = rest, None
    head, colon, tail = rest.rpartition(":")
    if colon and head:
        source, overlay = head, Path(tail).expanduser()
        if not overlay.is_dir():
            raise Refused(EXIT_INPUT, f"rules-ab: overlay {tail!r} of arm {name} is not a directory")
    if Path(source).expanduser().is_dir():
        kind = "dir"
    else:
        r = subprocess.run(["git", "-C", str(ENGINE_ROOT), "rev-parse", "--verify", "--quiet",
                            f"{source}^{{commit}}"], capture_output=True, text=True)
        if r.returncode != 0:
            raise Refused(EXIT_INPUT, f"rules-ab: arm {name}: {source!r} is neither a directory nor a git ref")
        kind = "ref"
    return {"name": name, "source": source, "kind": kind, "overlay": overlay}


def load_scenarios(path: str) -> list[dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Refused(EXIT_INPUT, f"rules-ab: cannot read scenarios {path}: {exc}") from exc
    if not isinstance(data, list) or not data:
        raise Refused(EXIT_INPUT, "rules-ab: scenarios must be a non-empty JSON list")
    seen = set()
    for s in data:
        ok = (isinstance(s, dict) and isinstance(s.get("id"), str) and s["id"] not in seen
              and isinstance(s.get("prompt"), str) and s["prompt"].strip()
              and isinstance(s.get("criteria"), list) and s["criteria"]
              and all(isinstance(c, str) for c in s["criteria"])
              and isinstance(s.get("rules", []), list)
              and s.get("touch", "none") in ("none", "read", "edit", "write")
              and isinstance(s.get("setup", {}), dict)
              and all(isinstance(v, str) for v in s.get("setup", {}).values()))
        if not ok:
            raise Refused(EXIT_INPUT, f"rules-ab: invalid scenario {json.dumps(s)[:160]}")
        seen.add(s["id"])
    return data


def build_arm(arm: dict, dest: Path) -> None:
    try:
        if arm["kind"] == "dir":
            shutil.copytree(Path(arm["source"]).expanduser(), dest, ignore=COPY_IGNORE, symlinks=True)
        else:
            dest.mkdir(parents=True)
            archive = subprocess.Popen(["git", "-C", str(ENGINE_ROOT), "archive", arm["source"]],
                                       stdout=subprocess.PIPE)
            untar = subprocess.run(["tar", "-x", "-C", str(dest)], stdin=archive.stdout)
            archive.stdout.close()
            if archive.wait() != 0 or untar.returncode != 0:
                raise OSError(f"git archive {arm['source']} | tar -x failed")
    except OSError as exc:
        raise Refused(EXIT_COPY, f"rules-ab: arm {arm['name']}: copy failed: {exc}") from exc
    refuse_symlinks(dest, f"arm {arm['name']}")
    if arm["overlay"]:
        for rel in overlay_files(arm["overlay"]):
            target = checked_relpath(dest, rel, f"overlay file of arm {arm['name']}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(arm["overlay"] / rel, target)
    strip_copy(dest)


def ab_settings(copy: Path) -> tuple[dict, str]:
    return build_settings(copy, get_data_root(), ENGINE_ROOT, deny_extra=("Bash", "Task"))


def _text_of(resp) -> str:
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()


def judge(client, model: str, criteria: list[str], reply: str, files: str) -> dict | None:
    """The verdict `{"criteria": [...]}`, one entry per criterion, or None after JUDGE_ATTEMPTS."""
    crit = "\n".join(f"{i}. {c}" for i, c in enumerate(criteria))
    user = f"CHECKLIST:\n{crit}\n\nFINAL REPLY:\n{reply}\n\nFILES WRITTEN OR CHANGED:\n{files}"
    for attempt in range(JUDGE_ATTEMPTS):
        try:
            r = client.messages.create(
                model=model, system=JUDGE_SYSTEM, messages=[{"role": "user", "content": user}],
                **claude_models.request_params(model, max_tokens=900, effort="low", output_schema=JUDGE_SCHEMA))
            v = json.loads(_text_of(r))
        except Exception as exc:  # noqa: BLE001 - any transport or parse fault: retried, then unjudged
            print(f"rules-ab: judge attempt {attempt + 1} failed: {type(exc).__name__}: {exc}"[:200],
                  file=sys.stderr)
            continue
        got = sorted(c.get("index") for c in v.get("criteria", []))
        if got == list(range(len(criteria))):
            v["criteria"].sort(key=lambda c: c["index"])
            return v
        print(f"rules-ab: judge attempt {attempt + 1}: indices {got} do not match the checklist",
              file=sys.stderr)
    return None


def _client():
    import anthropic

    load_env()
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise Refused(EXIT_INPUT, "rules-ab: ANTHROPIC_API_KEY is not set (.env); the judge cannot run")
    return anthropic.Anthropic(api_key=key, default_headers={"User-Agent": user_agent()})


def cmd_ab(args) -> int:
    scenarios = load_scenarios(args.scenarios)
    arms = [parse_arm(a) for a in args.arm]
    if len({a["name"] for a in arms}) != len(arms):
        raise Refused(EXIT_INPUT, "rules-ab: arm names must be unique")
    policies = [Path(p).expanduser() for p in args.policy_file or []]
    for p in policies:
        if not p.is_file():
            raise Refused(EXIT_INPUT, f"rules-ab: policy file {p} does not exist")
    for arm in arms:
        if arm["overlay"]:
            for rel in overlay_files(arm["overlay"]):
                checked_relpath(COPY_PARENT / "x", rel, f"overlay file of arm {arm['name']}")
    for s in scenarios:
        for rel in s.get("setup", {}):
            checked_relpath(COPY_PARENT / "x", rel, f"setup key of scenario {s['id']}")
    client = _client()
    reason = auth_probe()
    if reason:
        raise Refused(EXIT_CLAUDE, f"rules-ab: {reason}")
    subject = claude_models.resolve(args.model, default_family="opus")
    jmodel = claude_models.resolve(args.judge)
    out = Path(args.out) if args.out else (data_write_dir("runtime:scratch/tmp") / "rules-ab"
                                           / f"{time.strftime('%Y%m%d-%H%M%S')}-{args.label}")
    out.mkdir(parents=True, exist_ok=True)
    run_root = new_copy_root()
    stop = threading.Event()
    try:
        policy_file = None
        if policies:
            policy_file = run_root / "policy.md"
            policy_file.write_text("\n\n".join(p.read_text(encoding="utf-8") for p in policies), encoding="utf-8")
        for arm in arms:
            build_arm(arm, run_root / f"arm-{arm['name']}")
        preflight(run_root, run_root / f"arm-{arms[0]['name']}", subject, ab_settings, AB_TOOLS, policy_file)
        jobs = [(s, a["name"], k) for s in scenarios for a in arms for k in range(args.reps)]
        rng = random.Random(args.seed) if args.seed is not None else random.SystemRandom()  # noqa: S311 - job order, not a secret
        rng.shuffle(jobs)

        def run(job):
            if stop.is_set():
                return None
            s, arm, rep = job
            copy = run_root / f"s-{arm}-{uuid.uuid4().hex[:8]}"
            shutil.copytree(run_root / f"arm-{arm}", copy)
            try:
                for rel, body in s.get("setup", {}).items():
                    f = checked_relpath(copy, rel, f"setup key of scenario {s['id']}")
                    f.parent.mkdir(parents=True, exist_ok=True)
                    f.write_text(body, encoding="utf-8")
                strip_copy(copy)
                before = snapshot(copy)
                settings, mode = ab_settings(copy)
                row = run_session(copy, s["prompt"], subject, settings, mode, policy_file=policy_file)
                files = changed_files(copy, before)
            finally:
                shutil.rmtree(copy, ignore_errors=True)
            if row["escaped"]:
                stop.set()
            verdict = None
            if not row["escaped"] and (row["reply"] or files != "(no files changed)"):
                verdict = judge(client, jmodel, s["criteria"], row["reply"], files)
            out_row = {"id": s["id"], "arm": arm, "rep": rep, "session": row["session"],
                       "injected": row["injected"],
                       "rules_injected": {r: r in row["injected"] for r in s.get("rules", [])},
                       "touch": s.get("touch", "none"), "reply": row["reply"], "files": files,
                       "checklist": s["criteria"],
                       "criteria": verdict["criteria"] if verdict else [],
                       "pass": None if verdict is None else all(c["pass"] for c in verdict["criteria"]),
                       "escaped": row["escaped"], "judge": jmodel, "subject": subject,
                       "policy_files": [p.name for p in policies], "exit": row["exit"],
                       "seconds": row["seconds"],
                       "escaped_paths": [w["file_path"] for w in row["writes"] if w["outside"]]}
            print(f"rules-ab: {s['id']:12} {arm:10} rep{rep} pass={out_row['pass']} "
                  f"injected={','.join(row['injected']) or '-'}" + ("  ESCAPED" if row["escaped"] else ""),
                  file=sys.stderr)
            return out_row

        with ThreadPoolExecutor(args.workers) as ex:
            rows = [r for r in ex.map(run, jobs) if r is not None]
    finally:
        stop.set()
        shutil.rmtree(run_root, ignore_errors=True)
    with open(out / "rows.jsonl", "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    (out / "run.json").write_text(json.dumps({
        "label": args.label, "arms": [{"name": a["name"], "source": a["source"],
                                       "overlay": str(a["overlay"]) if a["overlay"] else None} for a in arms],
        "baseline": arms[0]["name"], "reps": args.reps, "subject": subject, "judge": jmodel,
        "policy_files": [p.name for p in policies], "claude": _claude_version(),
        "jobs_planned": len(jobs), "rows": len(rows)}, indent=1) + "\n", encoding="utf-8")
    summary = summarize(rows, [a["name"] for a in arms], scenarios, jmodel, args.label)
    (out / "summary.txt").write_text(summary + "\n", encoding="utf-8")
    print(summary)
    print(f"rules-ab: wrote {out}", file=sys.stderr)
    escaped = [p for r in rows if r["escaped"] for p in r["escaped_paths"]]
    if escaped:
        for p in escaped:
            print(f"rules-ab: ESCAPED write outside the copy: {p}", file=sys.stderr)
        return EXIT_ESCAPE
    return EXIT_OK


def summarize(rows: list[dict], arms: list[str], scenarios: list[dict], jmodel: str, label: str) -> str:
    lines = [f"label={label} judge={jmodel} scenarios={len(scenarios)} rows={len(rows)}"]
    tot = {a: [0, 0, 0] for a in arms}
    for s in scenarios:
        line = [f"{s['id']:12}"]
        for a in arms:
            rs = [r for r in rows if r["id"] == s["id"] and r["arm"] == a and not r["escaped"]]
            p = sum(1 for r in rs if r["pass"])
            u = sum(1 for r in rs if r["pass"] is None)
            tot[a][0] += p
            tot[a][1] += len(rs) - u
            tot[a][2] += u
            line.append(f"{a} {p}/{len(rs) - u}" + (f" ({u} unjudged)" if u else ""))
        lines.append("  ".join(line))
    for a in arms:
        p, n, u = tot[a]
        lines.append(f"TOTAL {a}: {p}/{n} = {p / n:.1%}  unjudged {u}" if n else f"TOTAL {a}: nothing judged")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# rejudge and analyze
# ---------------------------------------------------------------------------


def _read_rows(path: Path) -> list[dict]:
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError) as exc:
        raise Refused(EXIT_INPUT, f"rules-ab: cannot read rows {path}: {exc}") from exc


def cmd_rejudge(args) -> int:
    src = Path(args.rows)
    rows = _read_rows(src)
    client = _client()
    jmodel = claude_models.resolve(args.judge, default_family="opus")

    def run(r):
        r2 = dict(r)
        verdict = None
        if not r["escaped"] and (r["reply"] or r["files"] != "(no files changed)"):
            verdict = judge(client, jmodel, r["checklist"], r["reply"], r["files"])
        r2.update({"judge": jmodel, "criteria": verdict["criteria"] if verdict else [],
                   "pass": None if verdict is None else all(c["pass"] for c in verdict["criteria"])})
        return r2

    with ThreadPoolExecutor(args.workers) as ex:
        out = list(ex.map(run, rows))
    dest = src.with_name("rows-rejudge.jsonl")
    with open(dest, "w", encoding="utf-8") as fh:
        for r in out:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    both = [(a, b) for a, b in zip(rows, out, strict=True) if a["pass"] is not None and b["pass"] is not None]
    row_agree = sum(a["pass"] == b["pass"] for a, b in both)
    crit = [(x["pass"], y["pass"]) for a, b in both for x, y in zip(a["criteria"], b["criteria"], strict=True)]
    crit_agree = sum(x == y for x, y in crit)
    print(f"judge1={rows[0]['judge'] if rows else '-'} judge2={jmodel} rows={len(rows)} both-judged={len(both)}")
    print(f"agreement per row: {row_agree}/{len(both)} = {row_agree / len(both):.1%}" if both
          else "agreement per row: no row scored by both judges")
    print(f"agreement per criterion: {crit_agree}/{len(crit)} = {crit_agree / len(crit):.1%}" if crit
          else "agreement per criterion: no criterion scored by both judges")
    print(f"rules-ab: wrote {dest}", file=sys.stderr)
    return EXIT_OK


def analyze_rows(rows: list[dict], arms: list[str], baseline: str) -> list[str]:
    lines = []
    escaped = [r for r in rows if r["escaped"]]
    lines.append(f"escaped rows (excluded from every total): {len(escaped)}")
    rows = [r for r in rows if not r["escaped"]]
    lines.append(f"unjudged rows: {sum(1 for r in rows if r['pass'] is None)}")
    ids = sorted({r["id"] for r in rows})

    def frac(rs):
        judged = [r for r in rs if r["pass"] is not None]
        return Fraction(sum(1 for r in judged if r["pass"]), len(judged)) if judged else None

    per = {(i, a): frac([r for r in rows if r["id"] == i and r["arm"] == a]) for i in ids for a in arms}
    failed = [i for i in ids if per[(i, baseline)] is not None and per[(i, baseline)] <= Fraction(1, 3)]
    valid = [i for i in ids if i not in failed]
    lines.append(f"instrument failures (baseline {baseline} pass fraction <= 1/3), kept in all-scenario "
                 f"totals, left out of valid-scenario totals: {', '.join(failed) or 'none'}")

    def total(sel, a):
        rs = [r for r in rows if r["id"] in sel and r["arm"] == a and r["pass"] is not None]
        p = sum(1 for r in rs if r["pass"])
        return f"{p}/{len(rs)}" + (f" = {p / len(rs):.1%}" if rs else "")

    for a in arms:
        lines.append(f"  {a:12} all scenarios {total(ids, a):18} valid scenarios {total(valid, a)}")
    lines.append("by touch:")
    for t in ("none", "read", "edit", "write"):
        sel = sorted({r["id"] for r in rows if r.get("touch") == t})
        if sel:
            lines.append(f"  {t:6} " + "  ".join(f"{a} {total(sel, a)}" for a in arms))
    lines.append("losses (baseline pass fraction minus arm pass fraction >= 2/3):")
    any_loss = False
    for a in arms:
        if a == baseline:
            continue
        for i in valid:
            b, x = per[(i, baseline)], per[(i, a)]
            if b is not None and x is not None and b - x >= Fraction(2, 3):
                any_loss = True
                lines.append(f"  {i}: {baseline} {b} vs {a} {x}")
    if not any_loss:
        lines.append("  none")
    return lines


def cmd_analyze(args) -> int:
    d = Path(args.dir)
    if not (d / "rows.jsonl").is_file():
        raise Refused(EXIT_INPUT, f"rules-ab: {d} holds no rows.jsonl")
    meta = {}
    if (d / "run.json").is_file():
        meta = json.loads((d / "run.json").read_text(encoding="utf-8"))
    rows = _read_rows(d / "rows.jsonl")
    arms = [a["name"] for a in meta.get("arms", [])] or sorted({r["arm"] for r in rows})
    baseline = args.baseline or meta.get("baseline") or arms[0]
    if baseline not in arms:
        raise Refused(EXIT_INPUT, f"rules-ab: baseline {baseline!r} is not among the arms {arms}")
    files = [d / "rows.jsonl"] + ([d / "rows-rejudge.jsonl"] if (d / "rows-rejudge.jsonl").is_file() else [])
    for f in files:
        rs = _read_rows(f)
        judges = sorted({r.get("judge", "?") for r in rs})
        print(f"== {f.name}  judge={','.join(judges)}  rows={len(rs)}  baseline={baseline}")
        print("\n".join(analyze_rows(rs, arms, baseline)))
    return EXIT_OK


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("loader", help="which tool calls inject a paths:-scoped rule")
    p.add_argument("--reps", type=int, default=1)
    p.add_argument("--only", default="", help="trial ids, plus 'compact' and 'subagent_write'")
    p.add_argument("--model", default="sonnet")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--out")
    p.set_defaults(func=cmd_loader)
    p = sub.add_parser("ab", help="real-session A/B over arms")
    p.add_argument("--scenarios", required=True)
    p.add_argument("--arm", action="append", required=True, metavar="NAME=REF_OR_DIR[:OVERLAY_DIR]")
    p.add_argument("--label", required=True)
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--policy-file", action="append", metavar="PATH")
    p.add_argument("--model", default="opus")
    p.add_argument("--judge", default="sonnet")
    p.add_argument("--seed", type=int)
    p.add_argument("--out")
    p.set_defaults(func=cmd_ab)
    p = sub.add_parser("rejudge", help="re-score saved rows with a second judge")
    p.add_argument("rows")
    p.add_argument("--judge", default="opus")
    p.add_argument("--workers", type=int, default=8)
    p.set_defaults(func=cmd_rejudge)
    p = sub.add_parser("analyze", help="totals, exclusions and losses for one run directory")
    p.add_argument("dir")
    p.add_argument("--baseline")
    p.set_defaults(func=cmd_analyze)
    args = ap.parse_args(argv)
    if getattr(args, "label", None) is not None and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", args.label):
        print("rules-ab: --label must be 1-64 letters, digits, dot, dash or underscore", file=sys.stderr)
        return EXIT_INPUT
    if getattr(args, "reps", 1) < 1 or getattr(args, "workers", 1) < 1:
        print("rules-ab: --reps and --workers must be at least 1", file=sys.stderr)
        return EXIT_INPUT
    try:
        return args.func(args)
    except Refused as exc:
        print(str(exc), file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
