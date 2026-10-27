#!/usr/bin/env python3
"""PreToolUse path-redirect hook: data-relative tool paths -> the data root.

HEADING OS engine/data separation. When the session runs from the engine clone
(`.heading-os`), the DATA directories (`context/`, `crm/`, `outputs`,
`knowledge/`, `threads/`, `plans/`, `_archive/`, `corporate/`) are
NOT physically present at cwd -- they live in the `.heading-os-data` sibling,
resolved by `get_data_root()`. Python code reaches them via the `get_*_dir()`
helpers; this hook is the *tool-layer twin*: it rewrites Claude's own
Read/Write/Edit/Grep/Glob paths that target a data dir so the tool operates on
the real file in the data root. The engine working tree therefore stays
byte-clean -- zero data dirs, zero symlinks, nothing to leak -- while the agent
still reads and writes data transparently using ordinary cwd-relative paths
(exactly what every SKILL.md already does).

`outputs` and `plans` are always redirected, and no DATA_DIRS name may be
softened back out: an engine-local copy under one of them would be shadowed by
the redirect (27 such files were moved into the data overlay on 2026-08-25).

Since 2026-09-14 the first-segment gate also names the NINE HEADING OS 2 family
roots (`canon`, `ledger`, `status`, `research`, `work`, `inbound`, `build`,
`trace`, `asset`), derived from `families.family_roots()` rather than typed. The
records under `outputs`, `knowledge/` and the rest are moving to
`<family>/<leaf>/<their old path>`, and a boundary naming only the old roots is
a wall with the new tree on the wrong side of it: an unredirected Write to a
work/deliverable path would succeed against the ENGINE tree, which is the public
repository. Deriving rather than listing is what keeps a
tenth family inside the boundary on the day it is added rather than on the day
someone remembers this file.

No-op when `get_data_root() == get_workspace_root()` (only when HEADING_OS_DATA
names the checkout itself): the relative path already resolves correctly, so
nothing is rewritten and the hook exits silently.

Only RELATIVE paths whose first path segment is a data dir are rewritten;
absolute paths and engine paths (`scripts/`, `.claude/`, `reference/`,
`config/`, `docs/`, `tests/`, ...) are left untouched. This is what makes the
`/prime` script-path slip impossible: `scripts/prime-health-parallel.py` is not
a data path, so it is never redirected and resolves to the engine copy.

Mechanism: PreToolUse `hookSpecificOutput.updatedInput` (Claude Code hooks spec,
code.claude.com/docs/en/hooks.md). The emitted `updatedInput` carries the FULL
original tool input with only the path field(s) replaced, so it is correct under
both merge and full-replace harness semantics.

Performance: the cheap data-prefix test runs BEFORE importing workspace utils, so
the common case (engine paths, absolute paths, non-path tools) exits without the
import cost.

Second job: name a path-scoped rule the harness will not load. Claude Code
matches a rule's `paths:` globs against the project's own files only, so a rule
about data-repository files never loads from an engine session. Such a rule
lists its data surface in a `data-scope:` frontmatter key, as registry names
: `<name>`, `group:<group>` or an `{account}` name, each optionally
followed by file-name globs (`group:output-dirs *.pdf`). When a Read, Write,
Edit or MultiEdit target lies under one of those addresses, the hook adds an
`additionalContext` line naming the rule, once per session per rule. Advisory
only: any failure in it is logged to stderr and the call proceeds exactly as the
redirect left it. (2026-10-16, engine audit F-7.C1; the glob-based advisory of
HOS-171 matched nothing after the C3 move.)

Classification: engine (workspace logic, shipped in the engine clone).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# The engine checkout this hook belongs to. On sys.path here, because
# `DATA_DIRS` derives its family half from a `scripts.` module.
_ENGINE_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ENGINE_ROOT))

# Data dirs that live under the DATA root (mirror of the engine .gitignore data
# block + the get_*_dir() seam). A relative path whose FIRST segment is one of
# these is redirected; everything else (engine code, absolute paths) is not.
#
# A name here is a redirect KEY, not a claim that the directory still exists.
# MEASURED 2026-09-25: the top-level `datastore` directory is gone from the data
# repository, its records under the family roots below. The key stays, because
# dropping it would send a stray relative write of that spelling into the ENGINE
# checkout, which is the one place a data file must never land.
#
# `corporate` is kept on the same ground and one more. MEASURED 2026-09-25
# (HOS-133): no `corporate/` directory exists under the engine clone or the
# data repository, the executive layer it held having been retired by HOS-130.
# But the engine's `.gitignore` still ignores `/corporate/`, so without this key
# a stray relative write of that spelling would land in the engine as an
# IGNORED file, which no commit gate, push scan or engine-only check can see.
# Drop the key only together with that ignore line.
_LEGACY_DATA_DIRS = frozenset(
    {"context", "crm", "outputs", "knowledge", "threads", "plans", "datastore",
     "_archive", "corporate"}
)

# The nine HEADING OS 2 family roots, DERIVED from `families.FAMILIES` rather
# than typed, so a family added there is inside this boundary the same day.
# Importing that module reads no file: MEASURED 2026-09-14, 1.4 ms cumulative
# over three runs against a ~55 ms hook, which keeps it inside the run-to-run
# noise the pathnorm import above was already measured against. Do NOT reach for
# `scripts/heading_os_2/resolve.py` here; that one parses the migration manifest.
#
# `build/` and `work/` are ordinary English words and could collide with an
# engine directory of the same name, which would make this hook redirect engine
# paths into the overlay. CHECKED 2026-09-14 against `git ls-files`: the engine
# tree has no top-level `build/` and no top-level `work/`, so there is nothing
# for either name to shadow.
from scripts.heading_os_2.families import family_roots, subject_roots  # noqa: E402

# The C3 subject-tree roots joined 2026-10-13, from `families.subject_roots()`,
# which reads them from the data registry's `subject_roots:` line (HOS-234), so
# a root added there is redirected with no edit here. Asked LAZILY, because
# answering needs the data root and the read costs this hook roughly the
# `scripts.utils.paths` import (MEASURED 2026-10-13: 7.5-10.9 ms over three
# fresh processes, load average 3-4.4) on every call, against the cheap prefix
# test the module docstring promises. Two short-circuits keep the common case
# free: a segment the static set already holds, and a segment naming a
# top-level entry of THIS engine checkout, which the registry refuses as a
# subject root (`registry_bootstrap.check_root_names`), so it can never be one.
# An absolute path has no first segment and never gets here.
_STATIC_DATA_DIRS = _LEGACY_DATA_DIRS | frozenset(family_roots())
_SUBJECT_ROOTS: frozenset[str] | None = None


def _is_data_dir(segment: str) -> bool:
    """Whether a first path segment names a data root: static, or a subject root."""
    global _SUBJECT_ROOTS
    if not segment:
        return False
    if segment in _STATIC_DATA_DIRS:
        return True
    if (_ENGINE_ROOT / segment).exists():
        return False
    if _SUBJECT_ROOTS is None:
        # A malformed registry line answers the engine default's full set, said
        # on stderr (`families.subject_roots`): this hook is registered on its
        # own, so a crash would exit 1, which the harness reads as non-blocking,
        # and the write would go ahead against the ENGINE path.
        _SUBJECT_ROOTS = frozenset(subject_roots()) | _store_heads()
    return segment in _SUBJECT_ROOTS


def _store_heads() -> frozenset[str]:
    """The first segment of the runtime store's base: the engine default's and the registry's.

    HOS-241, 2026-10-13: the default base `runtime-junk` was no key here, so a
    relative write spelled under it landed in the ENGINE checkout. Both bases,
    because the C3 move nests the store inside a subject root and a write spelled
    under either must reach the data root while the move is under way. Read with
    the standard library from the files themselves (`registry_bootstrap`), never
    typed; one that cannot be read is said and left out.
    """
    from scripts.utils import registry_bootstrap as boot
    from scripts.utils.paths import DataRootError, get_data_root

    heads = set()
    try:
        example = _ENGINE_ROOT.joinpath(*boot.EXAMPLE_NAMES_REL)
        heads.add(boot.read_store_line(example.read_text(encoding="utf-8"), str(example)))
        heads.add(boot.store_dirname_from_files(get_data_root(), _ENGINE_ROOT))
    except (DataRootError, OSError, boot.StoreKeyError) as exc:
        print(f"[data-path-redirect] runtime store name unreadable ({exc}); "
              f"only the names read so far are redirected", file=sys.stderr)
    return frozenset(h.split("/", 1)[0] for h in heads if h)


# The lexical collapse is shared with the personal-threads wall in
# `_dispatch.py` (`scripts/utils/pathnorm.py`): classifying a raw `..` path once
# sent `outputs/../scripts/foo.py` to `<data-root>/scripts/foo.py` (2026-08-23),  # leak-guard: ok (history: the 2026-08-23 escape)
# and a private copy in one hook let the other stay broken (MEASURED 2026-08-29).
from scripts.utils.pathnorm import normalize_rel as _normalize_rel  # noqa: E402


def _first_segment(path: str) -> str:
    """First segment of a NORMALIZED relative path ('' if absolute/empty/escaping)."""
    norm = _normalize_rel(path)
    return norm.split("/", 1)[0] if norm else ""


def _is_data_rel(path: str) -> bool:
    return _is_data_dir(_first_segment(path))


# Per-tool: which input fields can carry a data-relative path we should rewrite.
# Grep's `pattern` is a regex (never a path) so only its `path` is considered.
# Glob's `pattern` IS a path-glob, handled specially (see _candidate_paths).
_PATH_FIELDS = {
    "Read": ("file_path",),
    "Write": ("file_path",),
    "Edit": ("file_path",),
    "MultiEdit": ("file_path",),
    "NotebookEdit": ("notebook_path",),
    "Grep": ("path",),
    "Glob": ("path",),
}


def _path_value(tool_input: dict, field: str) -> str:
    """The string value of a path field, or '' for anything that is not a string.

    The TYPE of the path INSIDE tool_input. `main` guards the container and
    nothing guarded the field, so `{"file_path": 3}` reached `_is_data_rel` and
    raised an uncaught AttributeError on `.replace`. Measured 2026-08-31 driving
    the real hook: an int, a bool, a list, a dict and a float each exited 1 with
    a traceback.

    This hook is PreToolUse, which is why the field matters more here than in
    its PostToolUse neighbours. A traceback exit means the redirect does not
    happen and the tool then proceeds against the ENGINE path, so a write lands
    in the wrong repository. A field that is not a string names no path, so it
    is treated as no path and announced, never silently swallowed.
    """
    value = tool_input.get(field) or ""
    if not isinstance(value, str):
        print(f"[data-path-redirect] {field} was {type(value).__name__}, "
              "not a string; treated as no path", file=sys.stderr)
        return ""
    return value


def _candidate_paths(tool_name: str, tool_input: dict) -> bool:
    """Cheap pre-check: is there any data-relative path worth rewriting?
    Runs before the workspace import so the common case stays import-free."""
    for field in _PATH_FIELDS.get(tool_name, ()):
        if _is_data_rel(_path_value(tool_input, field)):
            return True
    # Glob with no explicit path but a data-prefixed pattern (e.g. "outputs/**").  # leak-guard: ok (guard vocabulary: a legacy relative prefix this hook redirects)
    if tool_name == "Glob" and not _path_value(tool_input, "path"):
        if _is_data_rel(_path_value(tool_input, "pattern")):
            return True
    return False


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
    except Exception as e:
        # Fail-open on unparseable stdin only: the harness contract is broken,
        # not an exfil attempt; blocking would wedge every tool call.
        print(f"[data-path-redirect] failed to parse input: {e}", file=sys.stderr)
        return 0
    if not isinstance(payload, dict):
        return 0
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})
    if not isinstance(tool_input, dict):
        return 0
    # The same guard as the line above, for the field beside it. `tool_name` is
    # used as a DICT KEY (`not in _PATH_FIELDS`, `_PATH_FIELDS[tool_name]`), so
    # an unhashable value does not merely mismatch, it raises. Measured
    # 2026-09-01 driving the real hook: `[]` and `{}` each exited 1 with
    # `TypeError: unhashable type`, while `3`, `True`, `null` and `""` matched
    # nothing and returned 0 correctly.
    #
    # This is the defect `_path_value` documents one layer down, in the file
    # that had just fixed it there: the container was type-checked, the field
    # next to it was not. The consequence is the same one that docstring names.
    # PreToolUse dying means the redirect never runs and the tool proceeds
    # against the ENGINE path, so a write lands in the wrong repository.
    if not isinstance(tool_name, str):
        print(f"[data-path-redirect] tool_name was {type(tool_name).__name__}, "
              "not a string; no tool matched, nothing redirected", file=sys.stderr)
        return 0
    if tool_name not in _PATH_FIELDS:
        return 0

    updated = _redirected_input(tool_name, tool_input)
    # The advisory never costs the redirect: its failure is logged and the call
    # proceeds exactly as the redirect left it.
    try:
        advice = _rule_advisory(tool_name, updated or tool_input, payload)
    except Exception as e:  # noqa: BLE001 - advisory only; logged, never raised
        print(f"[data-path-redirect] rule advisory skipped: {e!r}", file=sys.stderr)
        advice = None
    if updated is None and advice is None:
        return 0
    out: dict = {"hookEventName": "PreToolUse"}
    if updated is not None:
        out["updatedInput"] = updated
    if advice is not None:
        out["additionalContext"] = advice
    json.dump({"hookSpecificOutput": out}, sys.stdout)
    return 0


# ============================================================
# Path-scoped rules the harness cannot reach (F-7.C1)
# ============================================================

# Tools whose target is one file. A search is not the moment a rule about
# writing or citing a document applies.
_ADVISORY_TOOLS = ("Read", "Write", "Edit", "MultiEdit")


def _data_scopes() -> dict[str, list[tuple[str, tuple[str, ...]]]]:
    """`{rule file: [(registry name, file-name globs), ...]}` from every rule's `data-scope:`.

    Derived from the rules themselves, so a rule that adds the key is covered
    with no edit here. Read without a YAML import, stopping at the closing
    `---`, to keep the cost to a few small reads.
    """
    scopes: dict[str, list[tuple[str, tuple[str, ...]]]] = {}
    for rule in sorted((_ENGINE_ROOT / ".claude" / "rules").glob("*.md")):
        entries, in_scope = [], False
        with rule.open(encoding="utf-8") as fh:
            if fh.readline().strip() != "---":
                continue
            for line in iter(fh.readline, ""):
                if line.strip() == "---":
                    break
                if line.startswith("data-scope:"):
                    in_scope = True
                    continue
                if in_scope and line.lstrip().startswith("- "):
                    words = line.split("- ", 1)[1].strip().strip("\"'").split()
                    if words:
                        entries.append((words[0], tuple(words[1:])))
                elif in_scope and line.strip():
                    in_scope = False
        if entries:
            scopes[rule.name] = entries
    return scopes


def _scope_addresses(name: str, data_root: Path) -> list[Path]:
    """Every address a `data-scope:` entry names; a group answers each member."""
    from scripts.utils.accounts import addresses
    from scripts.utils.data_path import GROUP_SCHEME, group_names

    members = (group_names(name[len(GROUP_SCHEME):], data_root)
               if name.startswith(GROUP_SCHEME) else (name,))
    out: list[Path] = []
    for member in members:
        out.extend(addresses(member, data_root=data_root))
    return out


def _rule_matches(path: Path, entries, data_root: Path) -> bool:
    from fnmatch import fnmatch

    for name, globs in entries:
        if globs and not any(fnmatch(path.name, g) for g in globs):
            continue
        try:
            homes = _scope_addresses(name, data_root)
        except Exception as e:  # noqa: BLE001 - one bad name must not hide the rest
            print(f"[data-path-redirect] data-scope {name!r} unresolved: {e}",
                  file=sys.stderr)
            continue
        if any(path == h.resolve() or path.is_relative_to(h.resolve()) for h in homes):
            return True
    return False


def _rule_advisory(tool_name: str, tool_input: dict, payload: dict) -> str | None:
    """The advisory line for this call, or None. `main` catches and logs anything it raises."""
    if tool_name not in _ADVISORY_TOOLS:
        return None
    raw = _path_value(tool_input, "file_path")
    if not raw.startswith("/"):
        return None  # a relative data path arrives here already redirected
    if raw.startswith(f"{_ENGINE_ROOT}/"):
        return None  # an engine file: the harness matches `paths:` itself
    scopes = _data_scopes()
    if not scopes:
        return None

    import os
    import re
    from scripts.utils.checkpoint_paths import session_id, state_root
    from scripts.utils.workspace import get_data_root

    data_root = get_data_root().resolve()
    path = Path(raw).resolve()
    if not path.is_relative_to(data_root):
        return None

    state_dir = state_root(_ENGINE_ROOT)
    sid = re.sub(r"[^A-Za-z0-9_-]", "_", session_id(payload))[:80]
    state_file = state_dir / f"rule-advisory-{sid}.json"
    announced = set(json.loads(state_file.read_text(encoding="utf-8"))) \
        if state_file.is_file() else set()
    due = [rule for rule, entries in scopes.items()
           if rule not in announced and _rule_matches(path, entries, data_root)]
    if not due:
        return None

    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(announced | set(due))), encoding="utf-8")
    os.replace(tmp, state_file)
    names = ", ".join(str(_ENGINE_ROOT / ".claude" / "rules" / r) for r in due)
    return (f"Read before working on {path.name}: {names}. Its `data-scope:` "
            f"covers this data-repository file, and Claude Code does not match "
            f"rules against files outside the project, so it was not loaded. "
            f"Said once per session per rule.")


def _redirected_input(tool_name: str, tool_input: dict) -> dict | None:
    """The tool input with data-relative paths moved onto the data root, or None."""
    # Cheap path-prefix gate BEFORE importing workspace utils.
    if not _candidate_paths(tool_name, tool_input):
        return None

    # A data-relative path is present. Resolve the data root; no-op if it is
    # the checkout itself (data_root == workspace_root). The workspace root
    # reached sys.path at import time, for pathnorm.
    try:
        from scripts.utils.workspace import get_data_root, get_workspace_root
        data_root = get_data_root().resolve()
        ws_root = get_workspace_root().resolve()
    except Exception as e:
        # Cannot resolve roots -> do not rewrite (leave the call as-is). Logged.
        print(f"[data-path-redirect] root resolution failed: {e}", file=sys.stderr)
        return None
    if data_root == ws_root:
        return None  # data in-tree; relative path already correct

    def _redirect(p: str) -> str | None:
        # Join the NORMALIZED path, never the raw one: `outputs/../../x` used to  # leak-guard: ok (history: the 2026-08-23 escape)
        # be classified data-relative on its raw first segment and then
        # concatenated verbatim, landing outside the data root.
        norm = _normalize_rel(p)
        if norm and _is_data_dir(_first_segment(p)):
            return str(data_root / norm)
        return None

    updated = dict(tool_input)
    changed = False
    for field in _PATH_FIELDS[tool_name]:
        new = _redirect(_path_value(updated, field))
        if new is not None:
            updated[field] = new
            changed = True
    # Glob with a data-prefixed pattern and no path: anchor the search at the
    # data root so the (still-relative) pattern resolves under it.
    if tool_name == "Glob" and not _path_value(tool_input, "path"):
        if _is_data_rel(_path_value(tool_input, "pattern")):
            updated["path"] = str(data_root)
            changed = True

    return updated if changed else None


if __name__ == "__main__":
    raise SystemExit(main())
