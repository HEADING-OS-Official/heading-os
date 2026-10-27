"""Refuse a runtime write into the data overlay that does not go through the front door.

Two halves, one module.

The STATIC half walks every tracked Python file under `scripts/` and `.claude/`
with the AST and derives each write site whose path is built from a data-root
helper plus literals: `open(..., "w"/"a"/"x")`, `write_text`, `write_bytes`,
`touch`, `mkdir`, `os.replace`, `shutil.copy*`/`move`, `logging.FileHandler` and
its rotating kin, `sqlite3.connect`, `tempfile` with `dir=`, and the engine's
own atomic-write and jsonl helpers. A site is REFUSED when the path it builds
is spelled the way runtime junk was spelled before 2026-09-14 (`_sync`, `.sync`,
`_tmp`, `.tmp`, `.cache`, `_derived`, a `logs/` or `state/` segment, a `*.lock`,
`*.log` or `*.jsonl` file, a `state.json` and its siblings, any `_`- or
`.`-prefixed segment the overlay's own .gitignore convention marks as junk)
and the chain does not start at the HOS-5 front door `scripts/utils/data_path.py`
(see `FRONT_DOOR`, added 2026-09-19 BEFORE any caller moved onto that seam: a
writer converted to it was invisible to this scan, 0 sites where it had had 1)
or at `runtime_root`. `scripts/utils/runtime_dir.get_runtime_dir` was the first
sanctioned root and was deleted under HOS-5 § 6 on 2026-09-25. A
record write (crm, auto-memory, knowledge, chronicle, threads,
the HEADING OS 2 families) carries none of those spellings and passes; the few
ledgers whose names look like junk are listed in `RECORD_EXEMPTIONS` with the
reason, and a row that matches nothing fails the test as stale.

The analysis is a small taint walk, not a grep. Names are scoped per function;
a function that returns a rooted path is a resolver for its callers, across
files when the caller imports it; a function that writes through one of its
parameters is a write call for its callers, with the argument as the path; a
path that climbs above the data root with `.parent` has left the overlay and
is dropped. MEASURED 2026-09-14 on main cf510b25, before any conversion: the
scan saw the rooted write sites and refused them, and after the conversions in
the same branch it refuses 0. A test pinned that count beside the date until
the 2026-09-21 test-suite split; the scan is unchanged, its pin is gone.

Git tracks the store's `state/` and `logs/` kinds and ignores `cache/` and
`tmp/` (HELM ruling 2026-09-14): a queue or an audit log placed here is still
durable, and the guard's spelling rules say nothing about that split, which is
the ledger's column, not the scanner's.

What the static half cannot see, stated rather than implied: a path that
arrives through a parameter not named `data_root`, `outputs_dir` or
`datastore_dir` when the caller is not in the corpus (`workspace_root` is
the ENGINE root in `scripts/utils/alert.py`, and the AST cannot tell an engine
root from a data root by name), a path
assembled from a shell string in a SKILL.md, a literal handed to a CLI as
`--output-dir`, a write a third-party library performs on paths handed to it as
environment variables, a store whose relative path travels through a dict of
targets (`memory-index.py`'s `_store_targets`, which the scan sees only as a
rooted `*`), and a write behind a helper whose name this module does not
list. Each of those is a row in the writers inventory that the conversion
handled by hand; the guard holds the shape it can see.

The RUNTIME half is a pytest plugin (armed by `tests/conftest.py` through
`pytest_plugins`, never a hook defined in conftest: two yards did that on
2026-09-13 and git merged both silently). Before each test it notes whether
the runtime store (`runtime_root`) exists under the OPERATOR's overlay (the `HEADING_OS_DATA`
the session started with, and the sibling `.heading-os-data`) and under the
engine checkout, with the mtime of the store root and of each service directory;
after the test it looks again and fails the test that changed either. The
hazard is specific to the operation this guards: `data_write_dir` CREATES
directories, `get_data_root()` in a test that forgot `scratch_data_root`
resolves the live overlay, and nothing else in the suite would say so. The
check is two levels of stat calls, so nothing a daemon writes INSIDE a kind
directory can disturb it.

Tests: one live, `test_no_exemption_row_forgives_a_write_site_that_does_not_exist`
in `tests/test_data_root_no_bypass.py` (every row forgives a real site, a
planted junk write is refused); the rest left in the 2026-09-21 split.
"""
from __future__ import annotations

import ast
import os
import re
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

from scripts.utils.repo_files import read_sources, tracked_python_files
from scripts.utils.resolver_closure import called_name, derived_resolvers

ENGINE_ROOT = Path(__file__).resolve().parents[2]

# ============================================================
# Vocabulary
# ============================================================

#: Parameter names that carry the data root into a function from outside the
#: corpus. `workspace_root` is deliberately absent: see the module docstring.
DATA_ROOT_PARAMS = frozenset({"data_root", "outputs_dir", "datastore_dir"})

#: The name outside the front door that makes a chain sanctioned. It was two
#: until 2026-09-25, when `get_runtime_dir` was deleted (HOS-5 § 6) and every
#: call to it moved onto `FRONT_DOOR` below.
SANCTIONED = frozenset({"runtime_root"})

#: Call names that pass a path through unchanged.
_PASSTHROUGH = frozenset({
    "Path", "PurePath", "PurePosixPath", "PosixPath", "str", "resolve",
    "absolute", "expanduser", "expandvars", "realpath", "abspath", "fspath",
})

#: The HEADING OS 2 seam (`scripts/heading_os_2/compat.py`), and the keyword
#: each one carries the legacy spelling in. These answer "where does the record
#: that used to live at X live now", so X is still written out at the call site
#: and it is the seam's ARGUMENT rather than its result that names the
#: directory. Read the argument or the needle is lost: a converted writer stops
#: matching its own exemption row instead of being forgiven by it, which turns
#: the fix into the failure (MEASURED 2026-09-18: four repaired writers went red).
_SEAM_LEGACY_ARG = {
    "write_dir_or_legacy": "legacy", "records_or_legacy": "legacy",
    "dirs_or_legacy": "legacy",
}

#: The HOS-5 front door, `scripts/utils/data_path.py`. Each of these dispatches
#: into a resolver that already owns the question -- `record:<family>/<leaf>`
#: into `heading_os_2.resolve`, `runtime:<service>/<kind>` into
#: the store's own vocabulary, a bare name into the registry under the data
#: root -- so a path built from one is rooted and SANCTIONED: the address was
#: answered by the owner of the vocabulary rather than spelled by hand.
#: `data_write_dir` is the fourth, since 2026-09-25: the same answer as
#: `data_path`, with the directory created, and the store root at 0700.
#: `data_write_file` is the fifth, the same day: the same answer, with the
#: FILE's parent created, for a registry name that places one file.
#:
#: The argument is the whole address and it is kept as ONE OPAQUE SEGMENT. Split
#: it on `/` and `runtime:browser/state` becomes `runtime:browser` then `state`,
#: and `state` is in `RUNTIME_SEGMENTS`: a CORRECT store address would spell as
#: junk. MEASURED 2026-09-19 on this tree, all three naive shapes, with
#: `data_path("runtime:browser/state") / "queue.jsonl"` as the writer:
#:
#:   `_SEAM_LEGACY_ARG["data_path"] = None`     -> 0 sites, 0 offending
#:   ...plus `_RESOLVER_DEPTH["data_path"] = 0` -> 0 sites, 0 offending
#:   `data_path` as a plain resolver            -> 1 site, REFUSED
#:
#: and `is_runtime_spelling(("runtime:browser", "state", "queue.jsonl"))` is
#: True. The first two are the guard going blind on a converted writer; the
#: third is the guard turning the conversion into the failure, which is the
#: shape `_SEAM_LEGACY_ARG`'s own comment records as the worst a gate can take.
#:
#: What this does NOT claim: a junk-spelled tail under a front-door address is
#: sanctioned and passes, exactly as a tail under `runtime_root` does. The
#: guard checks that the ADDRESS came from the seam, never that the file at the
#: end of it belongs at that address; the registry and the manifest answer that.
FRONT_DOOR = frozenset({"data_path", "data_read", "data_read_required", "data_write_dir",
                        "data_write_file"})

#: The chain a front-door site carries when the scanner could not read its
#: address AT ALL. Distinct from `_DYNAMIC`, and the distinction is the whole
#: value: `runtime:*/state` still names the vocabulary that answers, so the site
#: is honest about what it does not know. This one knows nothing.
#:
#: MEASURED 2026-09-20, before this existed, on `data_path(name) / "q.jsonl"`:
#: 1 rooted site, chain `("*", "q.jsonl")`, 0 offending -- indistinguishable
#: from `data_path(f"{x}")` and from a site whose directory really was computed.
#: Rooted, sanctioned, and with nothing in the chain that `is_runtime_spelling`
#: could ever call junk, so it could never be refused by any path this module
#: has. A guard that cannot be made to refuse a shape is not guarding it.
#:
#: The plan's phase-2 bullet splits this in two, an opaque expression against a
#: function parameter, and calls the second recoverable through the cross-file
#: pass. That split is true of `_SEAM_LEGACY_ARG`, where a parameter returns an
#: UNROOTED taint and falls into the deferred writers map. It is NOT true here,
#: and the difference is measured rather than argued: the front door's result is
#: rooted whatever you hand it, so the site is recorded in the callee at once.
#: MEASURED 2026-09-20 on a two-file corpus, `dump(p, name)` writing through
#: `data_path(name)` with a sibling calling `dump(p, "runtime:browser/state")`:
#: the scan reports one site, in the callee, with chain `("*", "q.jsonl")`. The
#: caller's literal never reaches it. So both shapes are the same blindness at
#: this branch and take one sentinel.
UNREADABLE_ADDRESS = "<unreadable-address>"

#: How many directories below the data root each resolver already stands.
#: A chain that climbs above the root with `.parent` has left the overlay and
#: is not this guard's business (any sibling tree).
_RESOLVER_DEPTH = {
    "get_data_root": 0, "env_data_root": 0, "require_writable_data_root": 0,
    "assert_data_root_external": 0,
}

#: Top-level packages an import can name and this scanner can follow.
_LOCAL_PACKAGES = frozenset({
    "scripts", "utils", "record_readers", "inbox_pulse",
    "heading_os_2",
})

#: Segments that were the junk spellings before the store existed, plus the
#: directory names the design's service table moves wholesale.
RUNTIME_SEGMENTS = frozenset({
    "_sync", ".sync", "_tmp", ".tmp", ".cache", "_derived", "logs", "state",
    ".memory-index", ".memory-index-code", ".latest", ".quarantine",
    "dead-letter", "action-queue", "firecrawl-cache", "clipboard", "browser",
    ".migration-backup", ".migration-staging", "_drafts", "_work", "compact-watch",
})

#: File suffixes that are junk wherever they sit under the overlay.
RUNTIME_SUFFIXES = (".lock", ".log", ".jsonl")

#: `_`- and `.`-prefixed names that are records or fixtures, not junk. The
#: overlay's .gitignore marks `_*` and `.*` under outputs/ as scratch; these are
#: the named exceptions to that convention.
RECORD_LIKE_PREFIXED = frozenset({
    "_archive", "_manifest", "_secure", "_shared", ".gitkeep", ".gitignore",
    ".schema-version", ".git", ".env", ".claude", "__pycache__", ".pen",
})

#: How each write call names its path: `receiver` for a pathlib method, an
#: argument index otherwise, `dir` for the tempfile family.
_RECEIVER = "receiver"
WRITE_CALLS: dict[str, object] = {
    "write_text": _RECEIVER, "write_bytes": _RECEIVER, "touch": _RECEIVER,
    "mkdir": _RECEIVER,
    "open": 0, "makedirs": 0,
    "replace": 1, "rename": 1, "copy": 1, "copy2": 1, "copyfile": 1,
    "copytree": 1, "move": 1,
    "FileHandler": 0, "RotatingFileHandler": 0, "TimedRotatingFileHandler": 0,
    "WatchedFileHandler": 0, "connect": 0,
    "mkstemp": "dir", "mkdtemp": "dir", "NamedTemporaryFile": "dir",
    "TemporaryDirectory": "dir",
    # the engine's own write helpers
    "atomic_write_text": 0, "atomic_write_bytes": 0, "atomic_write_json": 0,
    "save_json_atomic": 0, "write_text_atomic": 0, "append_jsonl": 0,
    "_append_jsonl": 0, "file_lock": 0, "quarantine_file": 0,
    "_atomic_write_json": 0, "_atomic_write": 0, "_atomic": 0,
}

_WRITE_MODE_CHARS = frozenset("wxa+")

#: The `os.open` flags that can modify a file. `O_RDONLY` is deliberately
#: absent: it is zero, and an expression naming only it is a read.
_WRITE_FLAGS = frozenset({"O_WRONLY", "O_RDWR", "O_CREAT", "O_APPEND", "O_TRUNC", "O_EXCL"})


def _flag_names(node: ast.AST) -> set[str]:
    """Every `O_*` name anywhere in a flags expression, however it is spelled."""
    found = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr.startswith("O_"):
            found.add(sub.attr)
        elif isinstance(sub, ast.Name) and sub.id.startswith("O_"):
            found.add(sub.id)
    return found
_DYNAMIC = "*"


# ============================================================
# Taint
# ============================================================

@dataclass(frozen=True)
class Taint:
    """What the scanner knows about one path expression."""
    rooted: bool = False          # built from a data-root helper or parameter
    sanctioned: bool = False      # the chain starts at the front door / runtime_root
    root: str = ""                # the helper or parameter that rooted it
    chain: tuple[str, ...] = ()   # the literal segments joined after the root
    param: str = ""               # unrooted: derives from this parameter of its function

    def extend(self, *segments: str) -> "Taint":
        return Taint(self.rooted, self.sanctioned, self.root, self.chain + tuple(segments), self.param)

    def rebase(self, base: "Taint") -> "Taint":
        """This taint, which derived from a parameter, with the argument's taint under it."""
        return Taint(base.rooted, base.sanctioned, base.root, base.chain + self.chain, base.param)


NOTHING = Taint()


def _rank(t: Taint | None) -> int:
    if t is None:
        return 0
    if t.rooted and not t.sanctioned:
        return 4
    if t.rooted:
        return 3
    if t.param:
        return 2
    return 1


def _worse(a: Taint | None, b: Taint | None) -> Taint | None:
    """Of two branches, the one a refusal would care about."""
    return a if _rank(a) >= _rank(b) else b


def _segments_of_literal(text: str) -> tuple[str, ...]:
    return tuple(s for s in text.split("/") if s)


#: `%s`, `%(name)s`, `{}` and `{0}`: the hole a value was substituted into. In
#: `"%s/outputs/_sync/x.json" % root` the hole IS the root, so it contributes no
#: segment of its own and the segments after it are the spelling under it.
_PLACEHOLDER = re.compile(r"^(%[-#0-9 +.*]*[a-zA-Z]|%\([^)]*\)[-#0-9 +.*]*[a-zA-Z]|\{[^}]*\})$")


def _concat_under(rooted: Taint, other: ast.AST, other_taint: "Taint | None") -> Taint:
    """`rooted` with the spelling that was concatenated onto it joined on.

    MEASURED 2026-09-14: before this existed, the `Add`/`Mod` branch returned
    `rooted.extend(_DYNAMIC)` and threw the literal away, so

        str(get_data_root()) + "/outputs/_sync/calendar/dump.ics"
        "%s/outputs/_sync/x.json" % root

    both evaluated to the chain `("*",)`. `is_runtime_spelling` skips `*`, so
    each was a rooted, unsanctioned, INVISIBLE junk write and `offending()`
    returned none. `/` was the only spelling the guard could actually read, and
    `+` is how a hurried conversion of sixty-nine writer rows gets written.

    The suffix case is why this is not a plain `extend`: `str(p) + ".lock"`
    grows the last segment rather than adding one, and reading it as a segment
    would invent a directory named `.lock`.
    """
    raw = other.value if isinstance(other, ast.Constant) and isinstance(other.value, str) else None
    if raw is None:
        return rooted.extend(*(other_taint.chain if other_taint and other_taint.chain else (_DYNAMIC,)))
    if "/" not in raw:
        if rooted.chain:
            return replace(rooted, chain=rooted.chain[:-1] + (rooted.chain[-1] + raw,))
        return rooted.extend(_DYNAMIC)
    segments = tuple(s for s in _segments_of_literal(raw) if not _PLACEHOLDER.match(s))
    return rooted.extend(*(segments or (_DYNAMIC,)))


# ============================================================
# Per-file structure, collected once
# ============================================================

@dataclass
class Scope:
    """One function body (or the module top level): its own bindings."""
    name: str
    params: tuple[str, ...] = ()
    defaults: dict[str, ast.AST] = field(default_factory=dict)
    names: dict[str, Taint] = field(default_factory=dict)
    assignments: list[tuple[str, ast.AST]] = field(default_factory=list)
    returns: list[ast.AST] = field(default_factory=list)
    calls: list[ast.Call] = field(default_factory=list)


@dataclass(frozen=True)
class FnInfo:
    """What callers may know about a function: its return, its parameters, and
    which parameters it writes through."""
    taint: Taint | None = None
    params: tuple[str, ...] = ()
    defaults: tuple[tuple[str, Taint], ...] = ()
    writers: tuple[tuple[str, tuple[str, ...]], ...] = ()   # (param, chain suffix)

    def default_taint(self, param: str) -> Taint | None:
        for name, taint in self.defaults:
            if name == param:
                return taint
        return None


@dataclass
class Env:
    """Bindings for one module and the cross-file map the evaluator consults."""
    resolvers: frozenset[str] = frozenset()
    module: Scope = field(default_factory=lambda: Scope("<module>"))
    scopes: list[Scope] = field(default_factory=list)
    attrs: dict[str, Taint] = field(default_factory=dict)
    functions: dict[str, FnInfo] = field(default_factory=dict)
    imports: dict[str, tuple[str, str]] = field(default_factory=dict)   # alias -> (file, name)
    modules: dict[str, str] = field(default_factory=dict)               # alias -> file
    global_functions: dict[tuple[str, str], FnInfo] = field(default_factory=dict)
    by_name: dict[str, list[FnInfo]] = field(default_factory=dict)      # bare name -> definitions
    names: dict[str, Taint] = field(default_factory=dict)               # the merged view, per scope

    def fn_info(self, name: str, receiver: ast.AST | None) -> FnInfo | None:
        if receiver is None and name in self.functions:
            return self.functions[name]
        if receiver is None and name in self.imports:
            return self.global_functions.get(self.imports[name])
        if isinstance(receiver, ast.Name) and receiver.id in self.modules:
            return self.global_functions.get((self.modules[receiver.id], name))
        if name in self.functions:
            return self.functions[name]
        candidates = self.by_name.get(name, [])
        if len(candidates) == 1:
            return candidates[0]
        return None


def _module_file(dotted: str, level: int, rel: str) -> str | None:
    """`scripts.utils.x` -> `scripts/utils/x.py`; relative imports off `rel`."""
    if level:
        base = Path(rel).parent
        for _ in range(level - 1):
            base = base.parent
        parts = list(base.parts) + (dotted.split(".") if dotted else [])
    else:
        parts = dotted.split(".")
    if not parts or parts[0] not in _LOCAL_PACKAGES:
        return None
    if parts[0] != "scripts":
        parts = ["scripts"] + parts
    return "/".join(parts) + ".py"


def collect(tree: ast.AST, rel: str = "<memory>") -> Env:
    """Read one module's structure. Done once; the fixpoints below reuse it."""
    env = Env()

    def bind(target: ast.AST, value: ast.AST, scope: Scope) -> None:
        if isinstance(target, ast.Name):
            scope.assignments.append((target.id, value))
        elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) \
                and target.value.id in ("self", "cls"):
            scope.assignments.append(("self." + target.attr, value))
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                bind(elt, value, scope)

    def visit(node: ast.AST, scope: Scope) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            ordered = list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
            inner = Scope(node.name, params=tuple(a.arg for a in ordered))
            positional = list(args.posonlyargs) + list(args.args)
            for arg, default in zip(positional[len(positional) - len(args.defaults):], args.defaults, strict=True):
                inner.defaults[arg.arg] = default
            for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
                if default is not None:
                    inner.defaults[arg.arg] = default
            for arg in ordered:
                if arg.arg in DATA_ROOT_PARAMS:
                    inner.names[arg.arg] = Taint(rooted=True, root=f"param:{arg.arg}")
                elif arg.arg not in ("self", "cls"):
                    inner.names[arg.arg] = Taint(param=arg.arg)
            env.scopes.append(inner)
            for child in node.body:
                visit(child, inner)
            return
        if isinstance(node, ast.ClassDef):
            # A class-body assignment is read back as `self.<name>` from every
            # method, but `bind` only records the `self.x = ...` form written
            # INSIDE a method. Until 2026-09-14 that left
            #     class Sync:
            #         junk = get_data_root() / "outputs" / "_sync"
            # binding `junk` in the MODULE scope, where the `self.junk` read at
            # `evaluate` looked in `env.attrs` and found nothing: the write was
            # never collected at all, so a rooted junk write was invisible to
            # the scan rather than merely unsanctioned. Both spellings of the
            # same assignment now reach `env.attrs`.
            for child in node.body:
                if isinstance(child, (ast.Assign, ast.AnnAssign)) and child.value is not None:
                    targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                    for target in targets:
                        if isinstance(target, ast.Name):
                            scope.assignments.append(("self." + target.id, child.value))
            for child in ast.iter_child_nodes(node):
                visit(child, scope)
            return
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                bind(target, node.value, scope)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            bind(node.target, node.iter, scope)
        elif isinstance(node, ast.Return) and node.value is not None:
            scope.returns.append(node.value)
        elif isinstance(node, ast.ImportFrom):
            file = _module_file(node.module or "", node.level, rel)
            if file:
                for alias in node.names:
                    env.imports[alias.asname or alias.name] = (file, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                file = _module_file(alias.name, 0, rel)
                if file:
                    env.modules[alias.asname or alias.name.split(".")[-1]] = file
        elif isinstance(node, ast.Call):
            scope.calls.append(node)
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(tree, env.module)
    return env


# ============================================================
# Evaluation
# ============================================================

def evaluate(node: ast.AST, env: Env) -> Taint | None:
    """The taint of one expression, or None when it is not path-shaped at all."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            return Taint(chain=_segments_of_literal(node.value))
        return None
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append(_DYNAMIC)
        return Taint(chain=_segments_of_literal("".join(parts)) or (_DYNAMIC,))
    if isinstance(node, ast.Name):
        return env.names.get(node.id)
    if isinstance(node, ast.Attribute):
        if node.attr in ("parent", "parents"):
            base = evaluate(node.value, env)
            return base.extend("..") if base else None
        if node.attr in ("name", "stem", "suffix"):
            base = evaluate(node.value, env)
            return Taint(chain=(_DYNAMIC,)) if base else None
        if isinstance(node.value, ast.Name) and node.value.id in ("self", "cls"):
            return env.attrs.get(node.attr)
        if isinstance(node.value, ast.Name) and node.attr in env.attrs:
            return env.attrs.get(node.attr)
        return None
    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Div):
            left = evaluate(node.left, env)
            if left is None:
                return None
            right = evaluate(node.right, env)
            return left.extend(*(right.chain if right and right.chain else (_DYNAMIC,)))
        if isinstance(node.op, (ast.Add, ast.Mod)):
            left = evaluate(node.left, env)
            right = evaluate(node.right, env)
            if left is None and right is None:
                return None
            if left and (left.rooted or left.param):
                return _concat_under(left, node.right, right)
            if right and (right.rooted or right.param):
                return _concat_under(right, node.left, left)
            # "a/b/c.json" + ".lock": the directory part stays, the leaf grows.
            head = left.chain[:-1] if left and left.chain else ()
            pieces = "".join(
                (t.chain[-1] if t and t.chain else _DYNAMIC) for t in (left, right))
            return Taint(chain=head + (pieces,))
        return None
    if isinstance(node, ast.Call):
        return _evaluate_call(node, env)
    if isinstance(node, ast.IfExp):
        return _worse(evaluate(node.body, env), evaluate(node.orelse, env))
    if isinstance(node, ast.BoolOp):
        out = None
        for value in node.values:
            out = _worse(out, evaluate(value, env))
        return out
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        out = None
        for elt in node.elts:
            out = _worse(out, evaluate(elt, env))
        return out
    if isinstance(node, ast.Dict):
        out = None
        for value in node.values:
            if value is not None:
                out = _worse(out, evaluate(value, env))
        return out
    if isinstance(node, ast.Subscript):
        return evaluate(node.value, env)
    if isinstance(node, ast.NamedExpr):
        return evaluate(node.value, env)
    if isinstance(node, ast.Await):
        return evaluate(node.value, env)
    return None


def _first_arg(node: ast.Call, index: int = 0, keyword: str | None = None) -> ast.AST | None:
    if len(node.args) > index:
        return node.args[index]
    if keyword:
        for kw in node.keywords:
            if kw.arg == keyword:
                return kw.value
    return None


def _argument_for(node: ast.Call, info: FnInfo, param: str, env: Env) -> Taint | None:
    """The taint the caller passes for `param`, or the parameter's default."""
    if param in info.params:
        index = info.params.index(param)
        expr = _first_arg(node, index, param)
    else:
        expr = _first_arg(node, 99, param)
    taint = evaluate(expr, env) if expr is not None else None
    if taint is None:
        taint = info.default_taint(param)
    return taint


def _evaluate_call(node: ast.Call, env: Env) -> Taint | None:
    name = called_name(node)
    if name is None:
        return None
    receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
    if name in SANCTIONED:
        chain = []
        for arg in node.args[:2]:
            t = evaluate(arg, env)
            chain.append(t.chain[-1] if t and t.chain else _DYNAMIC)
        return Taint(rooted=True, sanctioned=True, root=name, chain=tuple(chain))
    if name in FRONT_DOOR:
        # One segment, rejoined after `evaluate` split the literal on `/`. See
        # FRONT_DOOR: the split is what turns a correct store address into a
        # junk spelling. An address this cannot read is `*`, which every
        # classifier here already skips, so an unreadable name narrows the claim
        # instead of inventing a refusal.
        target = _first_arg(node, 0, "name")
        addressed = evaluate(target, env) if target is not None else None
        # Readable means at least ONE segment the scanner actually read. A chain
        # of nothing but `*` is an address it invented a shape for, which is the
        # same as no address; see UNREADABLE_ADDRESS.
        readable = (addressed is not None and addressed.chain
                    and any(seg != _DYNAMIC for seg in addressed.chain))
        spelled = "/".join(addressed.chain) if readable else UNREADABLE_ADDRESS
        return Taint(rooted=True, sanctioned=True, root=name, chain=(spelled,))
    if name in _SEAM_LEGACY_ARG:
        # BEFORE the resolver branch, deliberately. `derived_resolvers()` reaches
        # these by their return type, so the branch below would answer
        # `root="moved_file", chain=()` and the spelling in the argument would
        # never be read. Falls THROUGH to that branch when the argument yields
        # nothing, so a seam call this cannot see through is still a root rather
        # than an unrooted path the guard stops watching.
        #
        # The receiver is NOT consulted, unlike `_PASSTHROUGH` below: these are
        # module functions, so `workspace.moved_file(legacy, ...)` carries the
        # module name as its receiver and reading that would drop the argument
        # that holds the whole spelling.
        keyword = _SEAM_LEGACY_ARG[name]
        target = (_first_arg(node) if keyword is None
                  else _first_arg(node, 99, keyword))
        legacy = evaluate(target, env) if target is not None else None
        if legacy is not None:
            return legacy
    if name in env.resolvers:
        return Taint(rooted=True, root=name)
    if name in _PASSTHROUGH:
        target = receiver if receiver is not None else _first_arg(node)
        return evaluate(target, env) if target is not None else None
    if name == "joinpath" and receiver is not None:
        base = evaluate(receiver, env)
        if base is None:
            return None
        for arg in node.args:
            t = evaluate(arg, env)
            base = base.extend(*(t.chain if t and t.chain else (_DYNAMIC,)))
        return base
    if name in ("with_name", "with_suffix", "with_stem") and receiver is not None:
        base = evaluate(receiver, env)
        if base is None:
            return None
        arg = _first_arg(node)
        t = evaluate(arg, env) if arg is not None else None
        piece = t.chain[-1] if t and t.chain else _DYNAMIC
        return base.extend(f"<{name}:{piece}>")
    if name == "format" and receiver is not None:
        base = evaluate(receiver, env)
        return Taint(chain=(_DYNAMIC,)) if base else None
    if name == "join" and node.args:
        base = evaluate(node.args[0], env)
        if base is None:
            return None
        for arg in node.args[1:]:
            t = evaluate(arg, env)
            base = base.extend(*(t.chain if t and t.chain else (_DYNAMIC,)))
        return base
    info = env.fn_info(name, receiver)
    if info is not None and info.taint is not None:
        if info.taint.param:
            base = _argument_for(node, info, info.taint.param, env)
            return info.taint.rebase(base) if base is not None else Taint(chain=info.taint.chain)
        return info.taint
    if receiver is not None:
        base = evaluate(receiver, env)
        if base is not None and (base.rooted or base.param):
            return base  # an unknown method on a rooted path keeps the root
    return None


# ============================================================
# The per-module fixpoint
# ============================================================

def _enter(env: Env, scope: Scope) -> None:
    env.names = {**env.module.names, **scope.names}


def resolve(env: Env, resolvers: frozenset[str],
            global_functions: dict[tuple[str, str], FnInfo] | None = None,
            by_name: dict[str, list[FnInfo]] | None = None) -> Env:
    """Bindings for one module, iterated to a fixpoint.

    Names are scoped per function, so a `root` bound to the engine checkout in
    one function never roots a path in another (MEASURED 2026-09-14: a
    file-wide first draft rooted `scripts/sentinel.py`'s `.sentinel/` through
    a local name `get_workspace_root` shares with `env_data_root`, and 182 of
    its 437 sites were of that shape). `self.x` attributes stay module-wide,
    because that is what they are.
    """
    env.resolvers = resolvers
    env.global_functions = dict(global_functions or {})
    env.by_name = dict(by_name or {})
    for _round in range(8):
        changed = False
        for scope in [env.module] + env.scopes:
            _enter(env, scope)
            for name, value in scope.assignments:
                t = evaluate(value, env)
                if t is None:
                    continue
                if name.startswith("self."):
                    attr = name[5:]
                    merged = _worse(env.attrs.get(attr), t)
                    if merged != env.attrs.get(attr):
                        env.attrs[attr] = merged
                        changed = True
                    continue
                merged = _worse(scope.names.get(name), t)
                if merged != scope.names.get(name):
                    scope.names[name] = merged
                    env.names[name] = merged
                    changed = True
            if scope is not env.module:
                t = None
                for expr in scope.returns:
                    t = _worse(t, evaluate(expr, env))
                _enter(env, env.module)
                defaults = tuple(
                    (p, d) for p, d in ((p, evaluate(e, env)) for p, e in scope.defaults.items())
                    if d is not None)
                old = env.functions.get(scope.name)
                writers = old.writers if old else ()
                new = FnInfo(_worse(old.taint if old else None, t), scope.params, defaults, writers)
                if new != old:
                    env.functions[scope.name] = new
                    changed = True
        if not changed:
            break
    return env


# ============================================================
# Write sites
# ============================================================

@dataclass(frozen=True)
class WriteSite:
    file: str          # engine-relative, posix
    line: int
    call: str          # the write primitive
    root: str          # helper or parameter that rooted the path
    chain: tuple[str, ...]
    sanctioned: bool

    @property
    def spelled(self) -> str:
        return "/".join(self.chain)

    def describe(self) -> str:
        return f"{self.file}:{self.line} {self.call}() <- {self.root}()/{self.spelled}"


def _open_mode_is_write(node: ast.Call, receiver: bool) -> bool:
    index = 0 if receiver else 1
    mode = _first_arg(node, index, "mode")
    if mode is None:
        return False
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return bool(set(mode.value) & _WRITE_MODE_CHARS)
    flags = _flag_names(mode)
    if flags:
        # `os.open` takes O_* flags, not a mode string. Reading them is not
        # politeness: without this, `os.open(p, os.O_RDONLY)` fell through to
        # the assume-the-worse line below and the guard refused a READ. A
        # scanner that cries on a read is one an operator learns to wave
        # through, which costs more than the case it caught.
        return bool(flags & _WRITE_FLAGS)
    return True  # a computed mode: assume the worse


def _looks_like_module(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id in ("io", "os", "codecs", "gzip", "bz2", "lzma", "builtins")


def _path_argument(node: ast.Call, name: str) -> ast.AST | None:
    how = WRITE_CALLS[name]
    receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
    if name == "open":
        if receiver is not None and not _looks_like_module(receiver):
            return receiver if _open_mode_is_write(node, receiver=True) else None
        return _first_arg(node, 0, "file") if _open_mode_is_write(node, receiver=False) else None
    if how == _RECEIVER:
        return receiver
    if how == "dir":
        return _first_arg(node, 99, "dir")
    assert isinstance(how, int)
    if how == 1 and len(node.args) < 2:
        return _first_arg(node, 0)
    return _first_arg(node, how, "filename" if "Handler" in name else "database" if name == "connect" else None)


def inside_overlay(root: str, chain: tuple[str, ...]) -> tuple[str, ...] | None:
    """Normalise `..` and answer None when the chain climbs out of the data root."""
    depth = _RESOLVER_DEPTH.get(root, 1)
    out: list[str] = []
    for seg in chain:
        if seg == "..":
            if out:
                out.pop()
            else:
                depth -= 1
                if depth < 0:
                    return None
        else:
            out.append(seg)
    return tuple(out)


def _targets(node: ast.Call, env: Env) -> list[tuple[str, Taint | None, tuple[str, ...]]]:
    """(call name, taint of the path it writes, chain suffix) for one call; usually one."""
    name = called_name(node)
    if name is None:
        return []
    receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
    if name in WRITE_CALLS:
        target = _path_argument(node, name)
        if target is None:
            return []
        taint = evaluate(target, env)
        if (taint is None or not (taint.rooted or taint.param)) and WRITE_CALLS[name] == 1 and node.args:
            # os.replace(tmp, final): the destination was checked; fall back to
            # the source so a tmp sibling still names its site.
            taint = evaluate(node.args[0], env)
        return [(name, taint, ())]
    info = env.fn_info(name, receiver)
    if info is None or not info.writers:
        return []
    return [(name, _argument_for(node, info, param, env), suffix) for param, suffix in info.writers]


def sites_in(env: Env, rel: str) -> tuple[list[WriteSite], dict[str, tuple[tuple[str, tuple[str, ...]], ...]]]:
    """The rooted write sites of one module, and the functions that write through a parameter."""
    sites: list[WriteSite] = []
    writers: dict[str, list[tuple[str, tuple[str, ...]]]] = {}
    for scope in [env.module] + env.scopes:
        _enter(env, scope)
        for node in scope.calls:
            for name, taint, suffix in _targets(node, env):
                if taint is None:
                    continue
                if taint.rooted:
                    chain = inside_overlay(taint.root, taint.chain + suffix)
                    if chain is not None:
                        sites.append(WriteSite(rel, node.lineno, name, taint.root, chain, taint.sanctioned))
                elif taint.param and scope is not env.module:
                    entry = (taint.param, taint.chain + suffix)
                    if entry not in writers.setdefault(scope.name, []):
                        writers[scope.name].append(entry)
    return sites, {k: tuple(v) for k, v in writers.items()}


# ============================================================
# The scan
# ============================================================

def _parse(rel: str, text: str) -> ast.AST | None:
    try:
        return ast.parse(text, filename=rel)
    except SyntaxError:
        return None


def scan(root: Path | None = None, files=None, resolvers: frozenset[str] | None = None,
         passes: int = 5) -> list[WriteSite]:
    """Every rooted write site under `scripts/` and `.claude/`.

    Cross-file: a function in file A that returns a rooted path is a resolver in
    file B when B imports it (by `from` or by module alias), or when A is the
    only file in the corpus that defines that name; a function that writes
    through a parameter is a write call for its callers the same way. The map
    is rebuilt until it stops changing (at most `passes` rounds).
    """
    root = root or ENGINE_ROOT
    if files is None:
        files = [Path(root) / p for p in tracked_python_files(("scripts", ".claude"), root)]
    resolvers = resolvers or derived_resolvers()
    envs: dict[str, Env] = {}
    for path, text in read_sources(files, errors="replace"):
        rel = Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
        tree = _parse(rel, text)
        if tree is not None:
            envs[rel] = collect(tree, rel)
    global_functions: dict[tuple[str, str], FnInfo] = {}
    by_name: dict[str, list[FnInfo]] = {}
    sites: list[WriteSite] = []
    for _ in range(passes):
        merged: dict[tuple[str, str], FnInfo] = {}
        names: dict[str, list[FnInfo]] = {}
        sites = []
        for rel, env in envs.items():
            resolve(env, resolvers, global_functions, by_name)
            found, writers = sites_in(env, rel)
            sites.extend(found)
            for name, info in env.functions.items():
                info = FnInfo(info.taint, info.params, info.defaults, writers.get(name, ()))
                env.functions[name] = info
                merged[(rel, name)] = info
                names.setdefault(name, []).append(info)
        if merged == global_functions:
            break
        global_functions = merged
        by_name = names
    sites.sort(key=lambda s: (s.file, s.line))
    return sites


# ============================================================
# Classification
# ============================================================

def is_runtime_spelling(chain: tuple[str, ...]) -> bool:
    """Does this literal chain spell runtime junk the pre-store way?"""
    for seg in chain:
        if seg.startswith("<with_"):
            inner = seg[seg.index(":") + 1:-1]
            if inner.endswith(RUNTIME_SUFFIXES) or inner.endswith("state.json"):
                return True
            continue
        if seg in (_DYNAMIC, ".."):
            continue
        if seg in RUNTIME_SEGMENTS:
            return True
        if seg.endswith(RUNTIME_SUFFIXES):
            return True
        if seg == "state.json" or seg.endswith("state.json") or "index.db" in seg:
            return True
        if seg[0] in "_." and seg not in RECORD_LIKE_PREFIXED and not seg.startswith("_*"):
            return True
    return False


#: Sites the scan sees, spells as junk, and this branch deliberately leaves
#: where they are. Each row: (file suffix, chain substring, reason). A row that
#: matches no site is stale and the test says so.
RECORD_EXEMPTIONS: tuple[tuple[str, str, str], ...] = (
    ("scripts/router-accuracy-nightly.py", "trend.jsonl",
     "router-accuracy trend is ledger/measurement + status/machine (design 2026-09-14)"),  # leak-guard: ok (a reason, not a path)
    ("scripts/recall-calibration.py", "trend.jsonl",
     "memory-calibration trend is ledger/measurement + status/machine (design 2026-09-14)"),  # leak-guard: ok (a reason, not a path)
    # Row for `scripts/update-manager.py` dropped 2026-09-23: matched 0 sites once
    # `state_path()` moved onto the front door.
    ("scripts/ops-radar.py", "ack.json",
     "ops-radar's ack and crunch files are status/machine, and since 2026-09-18 "  # leak-guard: ok (a reason, not a path)
     "`resolve_state_dir` resolves them there instead of into the outputs tree the "
     "migration classifies as the (\"trace\", \"scratch\") kind. The row stays, and what it forgives is "  # leak-guard: ok (a reason naming a kind, not a path)
     "narrower than it was: MEASURED 2026-09-18, of the two sites it matches only the "
     "`.lock` beside the record is spelled junk, by the suffix alone, and dropping the "
     "row leaves exactly that one site refused. A lock sits where the record it guards "
     "sits; moving it into the junk store would put the lock and the file it serialises "
     "in two different trees. The bare `ack.json` site is not junk-spelled at either "
     "address and is forgiven by this needle without needing to be"),
    ("scripts/council-models-notify.py", "freshness-nudge-state.json",
     "council/freshness-nudge-state.json was left where it was reasoned 'status/machine "  # leak-guard: ok (a reason, not a path)
     "(design 2026-09-14); seam not serving', and on 2026-09-22 the second half expired. The "
     "classification was right and is unchanged: machine state, a dedup signature rebuilt from "
     "the findings each run, whose loss only re-announces a nudge, by `_save_signature`'s own "
     "docstring. What blocked the repair was that `get_runtime_dir('council-models-notify', "
     "'state')` raised `UnknownService`, and the row it asked for lives in the registry under "
     "the operator's DATA root, which is his configuration to change. He added it that day, so "
     "the seam serves this file and `_state_path()` resolves through it now. The row is kept "
     "because the scan still spells the site with this needle and the write still happens; what "
     "it forgives is no longer a write into a vacated directory. MEASURED 2026-09-22 after the "
     "conversion: this file contributes 3 rooted sites, all three sanctioned, 2 of them still "
     "matching this needle, and the whole scan reports 439 sites and 0 offending. Two matches "
     "is why the row is not deleted: this table's rule is that a row matching NO site is stale, "
     "and the needle here is the FILE NAME, which the conversion keeps in the chain. A needle "
     "spelling a legacy DIRECTORY does not survive its own repair, and two rows in this table "
     "were in that state as of 2026-09-22 -- reported then, not touched by that change, and "
     "removed on 2026-09-23 by the follow-up this sentence invited. Their dated replacements "
     "stand where they did. The cost that "
     "forced the question is paid. The HEADING OS 2 move emptied "
     "`outputs/` on 2026-09-21 and this writer was the last thing re-creating `operations/"
     "council` under it, once a day; the collision had already been seen on 2026-09-18 and "
     "written down in `scripts/council-record-verdict.py`'s `council_dir()`, which found that "
     "directory holding zero council files and one `freshness-nudge-state.json` 'another writer "
     "had already recreated it with'. That writer was this one"),
    ("scripts/council-aggregate.py", "_aggregate.md",
     "a council deliverable the operator reads; not in the writers inventory (2026-09-14). "
     "The needle lost its `council/` on 2026-09-15: `aggregate_path()` now resolves through "
     "`compat.write_dir_or_legacy(..., sublocation=COUNCIL_SUBDIR)`, so the directory arrives "
     "as a parameter and the scan spells it `operations/*/_aggregate.md`. Same one write"),
    # Rows dropped when their writer moved onto the front door and the legacy
    # needle matched 0 sites (a front-door call has no legacy argument for
    # `_SEAM_LEGACY_ARG` to read): `council-record-verdict.py` 2026-09-23,
    # `scrutinize-replay.py` and `aggregate-crm.py` 2026-09-24,
    # `scripts/utils/scrutinize_record.py` 2026-09-25. Rows for the retired
    # HEADING OS 2 name proposer (2026-09-20) and mover (2026-09-28, HOS-166)
    # went with their sites.
    ("scripts/memory-index.py", "*.lock",
     "`store_lock(root, store_rel)` locks whatever store its SECOND argument names, and the scan "
     "follows only the first, so every one of these spells `<root>/*.lock` (2026-09-15). The real "
     "paths are `content_store_path()`, already in the runtime store's `memory-index/cache` through "
     "the front door, and `CODE_STORE_REL` in the engine clone, which is not overlay junk. "
     "This is the `_store_targets` row the module docstring names as hand-handled"),
    ("scripts/utils/data_path.py", UNREADABLE_ADDRESS,
     "`data_write_dir` is the front door's write operation: it takes the address as a VALUE "
     "and creates the directory `data_path` answered for it, after `ensure_store_root` has made "
     "the store root 0700. It is the one place in the engine where a front-door address is "
     "legitimately a variable at a write, the same way the door's own CLI is for "
     "`scripts/leak-guard.py`; every caller of it is scanned at its own call with its literal "
     "(HOS-5 section 6, 2026-09-25, when `get_runtime_dir` was deleted and its creation moved here)"),
    ("scripts/apply-wizard-answers.py", UNREADABLE_ADDRESS,
     "a rich question's `output:` is a registry name read from the question bank, so the "
     "address arrives as a VALUE; `_resolve_output_path` accepts it only when it is spelled "
     "as a name (`_REGISTRY_NAME_RE`), `data_path` refuses a name the registry does not hold, "
     "and the result is held inside the data root. Two sites: the `.tmp` write and its "
     "`os.replace` (2026-10-16)"),
)


def _row_matches(file_suffix: str, needle: str, site: WriteSite) -> bool:
    """Does one exemption row cover one site?

    THE table's matcher, and it is one function because two questions are asked
    of it: `exemption_for` asks it site-first to decide a refusal, and
    `stale_exemptions` asks it row-first to decide whether a row is forgiving
    anything at all. Those were two copies of `endswith` + `in` until
    2026-09-23, one of them in a test, and a test carrying its own copy of the
    condition it is checking passes while the real one has stopped agreeing with
    it.
    """
    return site.file.endswith(file_suffix) and needle in site.spelled


def exemption_for(site: WriteSite) -> str | None:
    for file_suffix, needle, reason in RECORD_EXEMPTIONS:
        if _row_matches(file_suffix, needle, site):
            return reason
    return None


def stale_exemptions(sites, table=None) -> list[tuple[str, str]]:
    """The `(file suffix, needle)` of every row that forgives no site in `sites`.

    The table's own rule made answerable. A row whose writer has moved is not
    merely untidy: the needle is a SUBSTRING of the chain and the file is an
    `endswith`, so the row goes on forgiving whatever comes to occupy that
    address next, and nothing announces it. MEASURED 2026-09-23 on this tree,
    two rows were in that state and both had been reported the day before and
    left.

    `table` is a parameter so a caller can ask this of a HISTORICAL table
    against today's sites. That is what lets a test tell a repair from a
    reference that moved under it: the pinned version's table and this one are
    then judged over the same corpus rather than over two different walks.
    """
    table = RECORD_EXEMPTIONS if table is None else table
    return [(suffix, needle) for suffix, needle, _reason in table
            if not any(_row_matches(suffix, needle, site) for site in sites)]


def _store_coordinates(chain: tuple[str, ...]) -> tuple[str, str] | None:
    """The `(service, kind)` a store chain names, or None when it names neither.

    The leading `*` is the data-root argument `runtime_root` was called with,
    which is not a segment of the path; the store root's own README has nothing
    after it and is nobody's service.
    """
    rest = list(chain)
    while rest and rest[0] == _DYNAMIC:
        rest.pop(0)
    if len(rest) < 2:
        return None
    return rest[0], rest[1]


def misrouted(sites) -> list[WriteSite]:
    """Sites rooted at `runtime_root` naming a service or a kind nobody owns.

    The front door validates both halves of a `runtime:` address and refuses
    `UnknownService` / `UnknownKind`, which is the whole of what makes
    `runtime_dir.py`'s promise true: a typo in a writer is a refusal and not a
    new folder nobody owns. `runtime_root` takes neither argument. It returns
    the store root and the caller spells the rest by hand, so

        runtime_root(get_data_root()) / "lificc" / "state" / "queue.jsonl"

    creates `lificc/state/` in the runtime store (`runtime_root`) and no runtime check ever fires.
    Both names sit in `SANCTIONED`, so until 2026-09-14 the scan said nothing
    either, and the promise held only for callers of the validating helper
    (then `get_runtime_dir`, deleted 2026-09-25; now the front door).

    This is deliberately NOT folded into `is_runtime_spelling`. That function
    asks "does this spell junk the pre-store way", which is a question about
    the OLD paths; a misrouted store path is well-formed modern spelling
    aimed at a folder with no owner. Two defects, two names.

    Readers legitimately reach through `runtime_root` (it does not create, which
    is why a reader wants it). What they may not do is misspell what they reach
    for. A service or kind computed at runtime is skipped: the AST cannot spell
    it, and guessing would refuse honest code.

    Since 2026-09-19 the same check covers the HOS-5 front door, because
    sanctioning `data_path` without it would have opened a second door onto the
    hole this function exists to close: `data_path("runtime:lificc/state")` is
    a rooted, SANCTIONED site naming a service nobody owns, and `offending()`
    reads `sanctioned` before anything else.

    The two roots reach the one vocabulary by different routes, and only one of
    them needs a parser. A `runtime_root` chain is path SEGMENTS, so
    `_store_coordinates` reads the service and the kind out of them. A
    front-door chain already holds the address as written, so it goes to
    `validate_address` unaltered. A parser for the second existed for a day and
    was deleted on 2026-09-20: it was a second copy of `validate_address`'s
    `runtime:` branch, and being a copy of only that branch it could not see a
    `record:` address at all, so `data_path("record:")` and
    `data_path("record:trace/reportt")` passed this function while
    `scripts/leak-guard.py` refused both of them.
    """
    from scripts.utils.data_path import RUNTIME_SCHEME, validate_address

    # The vocabulary moved under the DATA root on 2026-09-19 (HOS-5 § 7.2), and
    # this guard reads ENGINE source: it runs in CI, on a public clone and under
    # a test fixture that pins the data root at an empty scratch directory, none
    # of which has a registry to read. The live list when there is one and the
    # engine's own template when there is not -- that is not a fallback for a
    # WRITER, since a writer with no data root refuses and the front door
    # still does. It is the only authority available to a static scan of public
    # code, and public code may only name a service the engine ships anyway.
    #
    # That whole judgement lived HERE until 2026-09-19 and now lives once, in
    # `data_path.validate_address`, because two other guards were about to grow
    # their own copy of it. This function keeps the question -- which SITES are
    # misrouted -- and has stopped keeping the vocabulary.
    out = []
    for site in sites:
        if site.root == "runtime_root":
            coordinates = _store_coordinates(site.chain)
            if coordinates is None:
                continue
            service, kind = coordinates
            if _DYNAMIC in (service, kind):
                continue
            spelled = f"{RUNTIME_SCHEME}{service}/{kind}"
        elif site.root in FRONT_DOOR:
            # The address as written, handed to the same check. There is no
            # front-door coordinate parser here on purpose: one existed for a
            # day and was a second copy of `validate_address`'s `runtime:`
            # branch that could not see a `record:` address at all, so
            # `data_path("record:")` and `data_path("record:trace/reportt")`
            # passed this function while `scripts/leak-guard.py` refused both.
            spelled = site.chain[0] if site.chain else ""
            if not spelled or spelled == UNREADABLE_ADDRESS or _DYNAMIC in spelled:
                continue  # unreadable is `unreadable()`'s; computed is nobody's
        else:
            continue
        if validate_address(spelled) is None:
            continue
        out.append(site)
    return out


def unreadable(sites) -> list[WriteSite]:
    """Front-door sites whose address the scanner could not read at all.

    RECORDED rather than skipped, which is the difference this function is. The
    two other classes here are decided by READING the address: junk-spelled asks
    what the path says, misrouted asks whether the vocabulary places it. Neither
    question can be put to a site with no address, so before this existed such a
    site passed every one of them and the pass meant nothing.

    Refused rather than merely listed, because the alternative is a guard that
    prints a number nobody acts on. The repair is one line at the call: spell
    the name. Where an address genuinely arrives as a value -- the front door's
    own CLI is the one case in this engine, and `scripts/leak-guard.py` carries
    the same refusal with the same sanctioned inline marker as its way out --
    the site is one to look at rather than a policy to weaken.
    """
    return [s for s in sites
            if s.root in FRONT_DOOR and s.chain and s.chain[0] == UNREADABLE_ADDRESS]


def offending(sites) -> list[WriteSite]:
    """The sites the guard refuses: junk-spelled, misrouted, or unreadable."""
    junk = [s for s in sites
            if not s.sanctioned and is_runtime_spelling(s.chain) and exemption_for(s) is None]
    out = list(junk)
    for s in misrouted(sites) + unreadable(sites):
        if s not in out and exemption_for(s) is None:
            out.append(s)
    return out


def report(sites) -> str:
    bad = offending(sites)
    if not bad:
        return "runtime-write-guard: 0 offending sites"
    lines = [f"runtime-write-guard: {len(bad)} runtime write(s) bypass the front door:"]
    lines += ["  " + s.describe() for s in bad]
    return "\n".join(lines)


# ============================================================
# The pytest plugin: no test creates the store under the real overlay
# ============================================================

def watched_store_roots() -> tuple[Path, ...]:
    """The stores a test must not create or alter: the operator's overlay's and the engine's.

    Read at import, which under pytest is session start, BEFORE any per-test
    monkeypatch of `HEADING_OS_DATA`; that is the value the machine runs with.

    The store's directory is each root's registry name (`runtime_store:`, HOS-5
    F1) AND the engine's default name, both watched: a registry that cannot be
    read here still has a default store a test could create, and a relocated
    store is watched where the registry now puts it.
    """
    from scripts.utils.runtime_dir import RuntimeDirError, default_store_dirname, store_dirname

    default = default_store_dirname()
    bases = []
    env = os.environ.get("HEADING_OS_DATA")
    if env:
        bases.append(Path(env))
    bases += [ENGINE_ROOT.parent / ".heading-os-data", ENGINE_ROOT]
    roots = []
    for base in bases:
        try:
            names = (store_dirname(base), default)
        except RuntimeDirError as exc:
            print(f"runtime-write-guard: {base}'s registry names no readable store "
                  f"({exc}); watching the default `{default}/` only", file=sys.stderr)
            names = (default,)
        for name in names:
            if base / name not in roots:
                roots.append(base / name)
    return tuple(roots)


def store_snapshot(roots) -> tuple:
    """(root, mtime of the root, ((service, mtime), ...)) per root; (root, None) when absent."""
    out = []
    for root in roots:
        try:
            st = root.stat()
        except OSError:
            out.append((str(root), None))
            continue
        services = {}
        try:
            for child in root.iterdir():
                try:
                    services[child.name] = child.stat().st_mtime_ns
                except OSError:
                    services[child.name] = None
        except OSError:
            pass
        out.append((str(root), st.st_mtime_ns, tuple(sorted(services.items()))))
    return tuple(out)


def snapshot_drift(before, after) -> list[str]:
    """Human-readable lines for each root that changed."""
    lines = []
    for b, a in zip(before, after, strict=False):
        if b == a:
            continue
        root = b[0]
        if b[1] is None and a[1] is not None:
            lines.append(f"{root} was CREATED")
        elif b[1] is not None and a[1] is None:
            lines.append(f"{root} was REMOVED")
        else:
            lines.append(f"{root} changed (a service or kind directory appeared or vanished)")
    return lines


try:
    import pytest
except ImportError:  # pragma: no cover - the static half is importable without pytest
    pytest = None

if pytest is not None:
    _WATCHED = watched_store_roots()
    _BEFORE = pytest.StashKey[tuple]()

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_setup(item):
        item.stash[_BEFORE] = store_snapshot(_WATCHED)
        return (yield)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_teardown(item, nextitem):
        result = yield
        before = item.stash.get(_BEFORE, None)
        if before is not None:
            drift = snapshot_drift(before, store_snapshot(_WATCHED))
            if drift:
                pytest.fail(
                    "runtime-write-guard: this test touched the runtime store (`runtime_root`) of "
                    "a REAL tree instead of a scratch data root:\n  " + "\n  ".join(drift)
                    + "\n  Use the `scratch_data_root` fixture (or pass data_root=) so "
                    "data_write_dir creates nothing under the operator's overlay.",
                    pytrace=False)
        return result


__all__ = [
    "DATA_ROOT_PARAMS", "Env", "FRONT_DOOR", "FnInfo", "RECORD_EXEMPTIONS",
    "RUNTIME_SEGMENTS", "RUNTIME_SUFFIXES", "SANCTIONED", "Taint",
    "UNREADABLE_ADDRESS", "WRITE_CALLS", "WriteSite", "collect", "evaluate",
    "exemption_for", "inside_overlay", "is_runtime_spelling", "misrouted",
    "offending", "report", "resolve", "scan", "sites_in", "snapshot_drift",
    "stale_exemptions", "store_snapshot", "unreadable", "watched_store_roots",
]


if __name__ == "__main__":  # console: print the sites and the refusals
    found = scan()
    bad = set(offending(found))
    print(f"{len(found)} rooted write sites")
    if "--all" in sys.argv:
        for s in found:
            tag = "BAD " if s in bad else ("EXMP" if exemption_for(s) else "OK  ")
            print("  " + tag + " " + s.describe())
    print(report(found))
    # Printed always, not behind `--all`: a row forgiving nothing is invisible
    # by construction, so the one surface that could report it must not be one
    # nobody passes a flag to.
    stale = stale_exemptions(found)
    for suffix, needle in stale:
        print(f"  STALE exemption, matches no site: {suffix} :: {needle}")
    sys.exit(1 if bad or stale else 0)
