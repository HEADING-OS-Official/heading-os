#!/usr/bin/env python3
"""
codegraph-gate.py - Claude Code UserPromptSubmit hook, in front of CodeGraph.

Replaces the third-party `codegraph prompt-hook` registration (package
@colbymchenry/codegraph 1.6.0, which this repository does not own and does not
edit). That hook ran a full `codegraph_explore` whenever a code-shaped token of
the prompt existed ANYWHERE in the index. MEASURED 2026-09-27 against the index
of the main clone:

- a prompt holding only two documentation URLs yielded the tokens `platform`
  and `claude` from a hostname; `platform` matched five `import platform`
  nodes and nothing else, and that was enough to inject "Found 71 symbols
  across 5 files" of unrelated code;
- background-agent completion events arrive as user-turn text opening with
  `<task-notification>`, and each one drew about 15.8 KB of injection;
- its cap was 16,000 characters, above the harness's 10,000-unit cap on hook
  output, so the model received a ~2 KB preview while the wrapper told it to
  "treat returned source as already read"; and it echoed the whole prompt.

What this gate does instead, and why it is the smaller design. It never runs
explore and never ships source. It answers one question from the index itself
(read-only SQLite, no Node start-up): which code-shaped words of the message
name a DEFINITION in this project, a function, class, method or variable. It
injects those as pointers (name, kind, file:line, signature) and tells the
model to call `codegraph_explore` when it wants the source. So nothing can be
truncated into a false "already read", the output is bounded by construction,
and the prompt is never echoed. Post-filtering the package's explore output was
the alternative; it would have coupled this file to a markdown format the
package can change in any release, for a body that is mostly truncated anyway.

Order of refusals, each silent: an event the harness composed (anything
opening with `<`, and the non-operator forms `scripts/utils/provenance.py`
classifies, a brief excepted because a brief is a task); no index at or above
`cwd`; no code-shaped token left once URLs, e-mail addresses, hostnames and
slash-bearing paths are removed; no token naming a definition.

Fail-open: any error emits nothing and exits 0, with one line on stderr naming
the cause. A hook in front of every prompt must never cost the prompt.

Coupling, stated rather than discovered: the SQL reads the package's `nodes`
table (`name`, `kind`, `file_path`, `start_line`, `signature`), read from its
schema on 2026-09-27. A schema change raises, and the fail-open path logs it.
"""

import importlib.util
import json
import re
import sqlite3
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parent.parent.parent
PROVENANCE = REPO / "scripts" / "utils" / "provenance.py"

# Under the harness's 10,000-unit hook-output cap with room for the JSON
# envelope, so the injection is never persisted behind a preview.
MAX_CHARS = 8000
MAX_TOKENS = 24          # distinct candidates looked up per message
MAX_PER_NAME = 3         # locations listed for one name
# The definition kinds, fixed in the statement text.
_DEFINITIONS_SQL = (
    "SELECT name, kind, file_path, start_line, signature FROM nodes "
    "WHERE name = ? AND kind IN ('function', 'class', 'method', 'variable') "
    "ORDER BY file_path, start_line")
# The forms that reach this hook as a user turn and are still a task someone
# set. Everything else `provenance.classify` names was composed by a machine.
TASK_FORMS = ("operator", "brief")

# Removed before any token is extracted, so a hostname or a path segment never
# becomes a symbol candidate.
_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+|\bwww\.\S+", re.IGNORECASE)
_EMAIL = re.compile(r"\S+@\S+\.\S+")
_HOST = re.compile(
    r"\b(?:[a-z0-9-]+\.)+(?:com|org|net|io|ai|dev|app|co|gov|edu|info|me|uk|"
    r"ru|ae|de|fr|eu|us|so|sh|xyz|cloud)\b\S*", re.IGNORECASE)
_PATHLIKE = re.compile(r"\S*/\S*")

# The package's own candidate shapes (`extractCodeTokens`, read 2026-09-27):
# camelCase or snake_case runs, `name(` call form, `a.b` member access unless
# it is a document or data filename. Two additions: source-file extensions join
# the filename list, so `name.py` yields `name` and not `py`, and backtick spans
# count, because `x` is how a person names code in prose.
_IDENT = re.compile(r"[A-Za-z_$][\w$]*")
_CALL = re.compile(r"([A-Za-z_$][\w$]*)\(")
_MEMBER = re.compile(r"([A-Za-z_$][\w$]*)\.([A-Za-z_$][\w$]*)")
_DOC_DATA_EXT = re.compile(
    r"\.(md|markdown|txt|rst|json|ya?ml|toml|lock|csv|tsv|log|ini|cfg|conf|"
    r"env|xml|html?|png|jpe?g|gif|svg|pdf|py|js|ts|sh)$", re.IGNORECASE)
_BACKTICK = re.compile(r"`([A-Za-z_$][\w$.]*)\(?\)?`")


def _log(msg: str, exc: BaseException | None = None) -> None:
    tail = f": {exc}" if exc is not None else ""
    print(f"codegraph-gate: {msg}{tail}", file=sys.stderr)


def _form(prompt: str) -> str:
    """`operator`, `brief`, or the machine form the prompt opens with."""
    if prompt.lstrip().startswith("<"):
        # The shape rule `_QUEUE_ENVELOPE` in `_dispatch.py` uses: every
        # envelope the harness composes opens with `<`, including ones not
        # invented yet.
        return "envelope"
    spec = importlib.util.spec_from_file_location("_cg_provenance", PROVENANCE)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {PROVENANCE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.classify(prompt)


def sanitize(prompt: str) -> str:
    text = _URL.sub(" ", prompt)
    text = _EMAIL.sub(" ", text)
    text = _HOST.sub(" ", text)
    return _PATHLIKE.sub(" ", text)


def candidates(prompt: str) -> list[str]:
    """Code-shaped words of an already sanitized prompt, first seen first."""
    out: dict[str, None] = {}
    for m in _BACKTICK.finditer(prompt):
        for part in m.group(1).split("."):
            if part:
                out.setdefault(part)
    for m in _IDENT.finditer(prompt):
        w = m.group(0)
        if re.search(r"[a-z][A-Z]", w) or re.search(r"[A-Za-z0-9]_[A-Za-z0-9]", w):
            out.setdefault(w)
    for m in _CALL.finditer(prompt):
        out.setdefault(m.group(1))
    for m in _MEMBER.finditer(prompt):
        if not _DOC_DATA_EXT.search(m.group(0)):
            out.setdefault(m.group(1))
            out.setdefault(m.group(2))
    return list(out)[:MAX_TOKENS]


def find_index(cwd: Path) -> Path | None:
    for d in (cwd, *cwd.parents):
        db = d / ".codegraph" / "codegraph.db"
        if db.is_file():
            return db
    return None


def definitions(db: Path, names: list[str]) -> list[tuple]:
    # One fixed statement per name rather than an assembled IN list: the text of
    # the SQL never varies, and `idx_nodes_name` makes each lookup an index hit.
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
    try:
        rows = []
        for name in names:
            rows.extend(con.execute(_DEFINITIONS_SQL, (name,)).fetchall())
    finally:
        con.close()
    return rows


def render(rows: list[tuple], names: list[str], root: Path) -> str:
    by_name: dict[str, list[tuple]] = {}
    for row in rows:
        by_name.setdefault(row[0], []).append(row)
    head = ('<codegraph_pointers source="local-index">\n'
            "Definitions in this project's CodeGraph index whose names appear "
            "in the message. Pointers only: no source was read or injected.\n")
    tail = ("</codegraph_pointers>\n"
            f"To read them, call codegraph_explore with these names "
            f'(projectPath: "{root}").')
    lines: list[str] = []
    dropped = 0
    budget = MAX_CHARS - len(head) - len(tail) - 80
    for name in names:
        hits = by_name.get(name)
        if not hits:
            continue
        for _, kind, path, line, sig in hits[:MAX_PER_NAME]:
            sig = (sig or "").replace("\n", " ")
            entry = f"- `{name}` ({kind}) {path}:{line}"
            if sig:
                entry += f"  {sig[:160]}"
            if budget - len(entry) - 1 < 0:
                dropped += 1
                continue
            budget -= len(entry) + 1
            lines.append(entry)
        if len(hits) > MAX_PER_NAME:
            more = f"  ({len(hits) - MAX_PER_NAME} more definitions of `{name}`)"
            if budget - len(more) - 1 >= 0:
                budget -= len(more) + 1
                lines.append(more)
    if dropped:
        lines.append(f"({dropped} more not listed, to stay under the hook cap)")
    return head + "\n".join(lines) + "\n" + tail


def build(payload: dict) -> str:
    prompt = str(payload.get("prompt") or "")
    if not prompt.strip():
        return ""
    form = _form(prompt)
    if form not in TASK_FORMS:
        return ""
    db = find_index(Path(str(payload.get("cwd") or ".")).resolve())
    if db is None:
        return ""
    names = candidates(sanitize(prompt))
    if not names:
        return ""
    rows = definitions(db, names)
    if not rows:
        return ""
    return render(rows, names, db.parent.parent)


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            raise ValueError(f"payload is a {type(payload).__name__}")
        context = build(payload)
    except Exception as exc:  # noqa: BLE001 - a prompt hook must not break the turn
        _log("stayed silent after an error", exc)
        sys.exit(0)
    if context:
        # ensure_ascii off: an escaped non-ASCII character costs six units of
        # the hook-output cap where the character itself costs one.
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }}, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    main()
