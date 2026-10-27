#!/usr/bin/env python3
"""Enforcement guard: no code may join a workspace/engine root to a DATA directory.

HEADING OS engine/data separation invariant (spec
2026-06-12-heading-os-engine-data-separation-design.md under registry name
`superpowers-specs`, section "regression guard"): after cutover the engine clone (.heading-os) holds
NO data; every data read/write must resolve under the DATA root via the
get_*_dir() helpers (which route through get_data_root()).
Code that does `get_workspace_root() / "outputs"` or
`WORKSPACE_ROOT / "crm"` bypasses the seam and misroutes private data INTO the
engine clone -- both a correctness bug and a leak (no gitignore safety in the
engine for these dirs).

This test fails on any such bypass so the principle is enforced by default for all
NEW code: engine always clean, ALL data in the data root. The fix for a flagged
line is always to use the matching helper:

    get_workspace_root() / "outputs" / x   ->  data_path("<registry name>") / x
    WORKSPACE_ROOT / "crm" / "contacts"    ->  data_path("crm-contacts")
    ROOT / "threads" / t                   ->  data_path("threads-business") / t
    workspace_root / "knowledge"           ->  get_knowledge_dir()
    ... / "plans" / p                      ->  data_path("plans") / p
    ... / "datastore" / d                  ->  data_path("<registry name>") / d

The helper DEFINITIONS in scripts/utils/workspace.py + paths.py are the only place
these literals legitimately sit next to a root, so those files are exempt. Engine
dirs (reference/, config/, scripts/, docs/, .claude/, examples/, tests/) are NOT
data and are not flagged.
"""
import ast
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.utils.workspace import get_workspace_root  # noqa: E402
from scripts.heading_os_2.families import family_roots, subject_roots  # noqa: E402
# The GIT_* scrub every child git command here needs. It lives in
# scripts/canopus_check.py, whose docstring carries the reasoning; see
# `_clean_git_env` below for what this file measured without it.
from scripts.canopus_check import git_child_env  # noqa: E402

# The bare __file__-parent idiom that resolves to the ENGINE root for a script in
# scripts/ -- `Path(__file__).resolve().parent.parent` (or without .resolve()) and
# `os.path.dirname(os.path.dirname(...))`. The original guard knew the named-root
# tokens below but NOT this idiom, so a script doing `BASE =
# Path(__file__).resolve().parent.parent` then `Path(BASE) / "outputs"` slipped a
# data write into the engine clone undetected (5 doc/deck generators did exactly
# this -- 2026-06-16 finding #3). Both producer lists now include it.
_FILE_PARENT_PRODUCER = (
    r"Path\(__file__\)(?:\.resolve\(\))?(?:\.parent\.parent|\.parents\[\s*[1-9]\d*\s*\])"
    r"|os\.path\.dirname\(\s*os\.path\.dirname\("
)
# Root identifiers that resolve to the ENGINE/workspace root (not the data root).
# Includes the module-const ALIASES (WORKSPACE, WS) that the original migration
# missed -- a file doing `WORKSPACE = get_workspace_root()` then `WORKSPACE /
# "outputs"` bypasses the seam exactly like the explicit-token form.
_ROOT_TOKENS = r"(?:get_workspace_root\(\)|WORKSPACE_ROOT|workspace_root|\bWORKSPACE\b|\bWS\b|PROJECT_ROOT|PROJECT_DIR|\bROOT|" + _FILE_PARENT_PRODUCER + r")"
# DATA directories that must be reached via a get_*_dir() helper, never joined to
# an engine root directly. context/ resolves under the data root (registry
# names `context-personal` and the `context-homes` group), so it belongs here too.
#
# `auto-memory`, `chronicle`, `admin` and `personal` were added 2026-09-01.
# All four route `private` in `config/routing-map.yaml` for every rule that
# names them, all four live only in the DATA overlay, and none of them was in
# this alternation - so `get_workspace_root() / "auto-memory"` was a seam bypass
# this guard reported as clean. MEASURED that day: the shipped pattern returned
# False on `x = get_workspace_root() / "auto-memory" / "MEMORY.md"` and on the
# same line spelled with `chronicle`, while the `outputs` spelling one character
# away was caught. `auto-memory` is the memory store the workspace calls its own
# second brain; misrouting it into the engine clone puts operator memory in a
# public repository.
#
# Widening cost nothing: with all four added, the line scan and the cross-line
# binder both returned zero hits across 442 files, so none of this is a new
# false positive on existing code.
#
# `docs`, `config`, `scripts`, `reference` and `.claude` are deliberately absent:
# each defaults to `engine` with per-file `private` carve-outs, so joining one
# to an engine root is ordinary correct code.
#
# Widened 2026-09-19 with the nine HEADING OS 2 family roots, DERIVED from
# `families.family_roots()` and never typed, which is what the two sibling
# guards on this same question already did on 2026-09-14:
# `scripts/leak-guard.py` line 63 and `scripts/audit-skill-bash-paths.py`
# line 79 both build their vocabulary from that call. This file listed eleven
# names by hand and none of the nine was among them, so it widened when
# somebody remembered it and the siblings widened by themselves. MEASURED
# 2026-09-19 in this checkout: `canon`, `ledger`, `status`, `research`, `work`,
# `inbound`, `build`, `trace`, `asset` were all absent, so
# `get_workspace_root() / "trace" / "report"` -- the exact shape records have
# been moving into since 2026-09-14 -- was reported CLEAN by this guard while
# its two siblings refused the same spelling.
#
# Nothing is leaking today and this is not a repair of a live defect: MEASURED
# the same day in HELM, no data directory of any of the fifteen names exists in
# the engine clone, and across 1,951 tracked Python files there is not one line
# joining an engine root to a family root. The hole is that yesterday's
# vocabulary is what is guarded, and it has to be closed BEFORE HOS-5 phase 3,
# which is the phase that moves thousands of call sites onto the new seam and
# in which such a line arrives by ordinary accident.
#
# Two of the nine, `build` and `work`, are ordinary English words. The pattern
# is not a substring scan: it requires an ENGINE-ROOT token joined to a QUOTED
# literal starting with the name, so `"rebuild"`, `"buildings"` and
# `"https://x/build/y"` cannot reach it, and `\b` is what stops `"builder"`.
# MEASURED after the widening: 0 new violations across the scanned tree, so
# none of this is a new false positive on existing code.
_DATA_DIRS = (r"(?:threads|crm|outputs|knowledge|plans|datastore|context"
              r"|auto-memory|chronicle|admin|personal"
              r"|" + "|".join((*subject_roots(), *family_roots())) + r")")
# Operator form: ROOT / "outputs"  or  ROOT + "outputs".
_BYPASS = re.compile(_ROOT_TOKENS + r"\s*(?:/|\+)\s*[\"']" + _DATA_DIRS + r"\b")
# os.path.join form: os.path.join(<...>, ROOT, "outputs", ...) -- the operator
# regex misses the comma separator, so this branch catches the join() bypass that
# previously slipped data writes into the engine tree (telegram download default,
# firecrawl cache, docx output).
_BYPASS_JOIN = re.compile(
    r"os\.path\.join\([^)]*" + _ROOT_TOKENS + r"\s*,\s*[\"']" + _DATA_DIRS + r"\b"
)
# joinpath() and f-string forms the operator/join regexes above miss. Restricted to
# UNAMBIGUOUS producer expressions -- a direct get_workspace_root() call or the
# __file__-parent idiom -- NOT the bare aliases (workspace_root/ROOT/WS/...). That
# restriction is deliberate: a benign INJECTED parameter like dead_letter.py's
# `workspace_root.joinpath("outputs")` (param default None; real path from the
# data root) must not be false-flagged. Alias vars that are PROVEN
# engine-bound are handled binding-aware in the cross-line test below, which adds
# the same two forms per collected var.
_DIRECT_PRODUCER = r"(?:get_workspace_root\(\)|" + _FILE_PARENT_PRODUCER + r")"
# get_workspace_root().joinpath("outputs")  /  (Path(__file__)...).joinpath("crm")
_BYPASS_JOINPATH = re.compile(_DIRECT_PRODUCER + r"\.joinpath\(\s*[\"']" + _DATA_DIRS + r"\b")
# f"{get_workspace_root()}/outputs/..."  (data dir interpolated under the engine root)
_BYPASS_FSTRING = re.compile(
    r"\{[^{}]*(?:" + _DIRECT_PRODUCER + r")[^{}]*\}/" + _DATA_DIRS + r"\b"
)

# Files allowed to contain the literal pattern: the helper definitions themselves,
# this guard, and dead archived code (never executed; not part of the live engine).
_EXEMPT_SUBSTRINGS = (
    "scripts/utils/workspace.py",
    "scripts/utils/paths.py",
    "tests/test_data_root_no_bypass.py",
    "/archive/",
)

# Producers that yield the ENGINE root (NOT the data root). A var bound to one of
# these and later joined to a data dir is a cross-line seam bypass the line-based
# _BYPASS regex misses (it cannot tell a data-root `root` from an engine-root one).
# The __file__-parent idiom is included so `BASE =
# Path(__file__).resolve().parent.parent; Path(BASE) / "outputs"` is caught.
_ENGINE_PRODUCER = r"(?:get_workspace_root\(\)|WORKSPACE_ROOT|workspace_root|PROJECT_ROOT|PROJECT_DIR|\bWORKSPACE\b|\bWS\b|" + _FILE_PARENT_PRODUCER + r")"
# Anchor the binding to assignment-statement position (line start or after `;`),
# so a keyword argument like `load_entity(slug, workspace_root=workspace_root)` is
# NOT mistaken for `workspace_root = <producer>`. Without the anchor, the bare
# `workspace_root` producer token matched the kwarg `name=value` form and falsely
# collected the var (false positive on scripts/utils/crm.py, 2026-06-16).
_ENGINE_BIND_RE = re.compile(r"(?:^|;)\s*(\w+)\s*=\s*" + _ENGINE_PRODUCER, re.MULTILINE)

# Names a regex binder cannot recognise, whatever the producer alternation says.
# `_ENGINE_BIND_RE` requires the producer to sit IMMEDIATELY after the `=`, which
# is the anchor that stopped `load_entity(slug, workspace_root=workspace_root)`
# being read as an assignment (2026-06-16). The anchor is right and it is also
# blind, and on 2026-08-29 the blindness cost something real:
#
#     _ws_root = Path(workspace_root) if workspace_root else _WORKSPACE_ROOT
#     _stages = parse_pipeline_stages(_ws_root / "context" / "pipeline.md")
#
# in `scripts/utils/crm.py`. Two independent reasons it was invisible. The
# producer is behind a TERNARY, so nothing engine-shaped follows the `=`; and it
# carries a LEADING UNDERSCORE, so even matching after `else` fails, because
# `WORKSPACE_ROOT` cannot match starting at the `_` of `_WORKSPACE_ROOT`.
# Measured: both the shipped binder and a widened `else`-aware one returned []
# on that line.
#
# The consequence was not theoretical. `context/pipeline.md` and `crm/aliases.md`
# are operator data and do not exist in the engine clone, so both reads resolved
# to nothing, and stage-aware CRM cadence had never once applied in production.
#
# The AST does not care about ternaries or underscores. It is ADDED beside the
# regex rather than replacing it, so the existing coverage and its survivor floor
# are untouched, and a parse failure degrades to the regex rather than to silence.
# CONSTANTS only, plus the two direct producers. A lower-case `workspace_root` is
# excluded deliberately: as a parameter it is a value the CALLER chose, which is
# the sanctioned fixture and exec-repo override, not the frozen engine root. The
# first version of this binder included it and produced three false positives,
# all of them correct code following the very pattern this guard recommends:
# `crm_autolog._address_book_dir` and `_contacts_dir` (seam when the argument is
# None, explicit tree when it is given) and the CRM aggregator (removed
# 2026-09-25), where `repo_path` was another executive's clone. `_WORKSPACE_ROOT` is a different thing entirely:
# a module constant computed from `__file__` that no caller can redirect.
_ENGINE_CONST_RE = re.compile(r"^_*(?:WORKSPACE_ROOT|PROJECT_ROOT|PROJECT_DIR|WORKSPACE|WS)$")


def _file_depth(node: "ast.AST", bound: dict[str, int]) -> int | None:
    """Parents climbed from `Path(__file__)`, or None when `node` is not that.

    `.parent` climbs one and `.parents[N]` climbs N + 1; `.resolve()` climbs
    none. A name in `bound` was assigned such an expression earlier, so
    `HERE = Path(__file__)` then `HERE.parents[1]` is the same climb (HOS-185:
    until 2026-10-03 only the `.parent` chain written on one line was seen).
    The argument is checked, not just the shape: `Path(other).parent.parent` is
    somebody else's tree and must not be called an engine root.

    The `os.path.dirname(os.path.dirname(...))` spelling is NOT handled here.
    The regex binder already covers it and still runs beside this one; a second
    partial copy would be the very shape this whole shard is about.
    """
    depth = 0
    cur = node
    while True:
        if isinstance(cur, ast.Attribute):
            if cur.attr == "parent":
                depth += 1
            elif cur.attr != "resolve":
                return None
            cur = cur.value
        elif (isinstance(cur, ast.Subscript) and getattr(cur.value, "attr", "") == "parents"
              and type(getattr(cur.slice, "value", None)) is int):  # a literal `[N]`, never `[-1]`
            depth, cur = depth + cur.slice.value + 1, cur.value.value
        elif isinstance(cur, ast.Call):
            if (isinstance(cur.func, ast.Name) and cur.func.id == "Path"
                    and len(cur.args) == 1
                    and isinstance(cur.args[0], ast.Name)
                    and cur.args[0].id == "__file__"):
                return depth
            cur = cur.func
        elif isinstance(cur, ast.Name) and cur.id in bound:
            return depth + bound[cur.id]
        else:
            return None


def _mentions_engine_producer(node: "ast.AST", bound: dict[str, int]) -> bool:
    """True when an expression reads an engine-root value anywhere inside it."""
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and _ENGINE_CONST_RE.match(child.id):
            return True
        # Two parents or more: one gets you `scripts/`, not the repository root.
        if (isinstance(child, ast.Attribute) and _ENGINE_CONST_RE.match(child.attr)
                or (_file_depth(child, bound) or 0) >= 2):
            return True
        if (isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
                and child.func.id == "get_workspace_root"):
            return True
    return False


def _engine_bound_vars_ast(text: str) -> set[str]:
    """Local names assigned an expression that mentions an engine-root producer.

    Keyword arguments cannot reach this: `f(x, workspace_root=y)` is a `Call`,
    never an `Assign`, so the false positive the regex anchor exists to prevent
    is structurally impossible here rather than defended against.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return set()  # the regex binder still covers this file
    found: set[str] = set()
    bound: dict[str, int] = {}  # name -> parents climbed from `Path(__file__)`
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if isinstance(targets[0], ast.Name) and _file_depth(value, bound) is not None:
            bound[targets[0].id] = _file_depth(value, bound)
        if not _mentions_engine_producer(value, bound):
            continue
        for target in targets:
            for name in ast.walk(target):
                if isinstance(name, ast.Name):
                    found.add(name.id)
    return found


def _bound_var_violations(text: str, rel: str) -> tuple[list[str], int]:
    """Engine-bound vars in `text` that are joined to a data directory.

    Extracted from the tree scan on 2026-08-29 so a unit test can drive the SAME
    code on a synthetic file. It was inline, and the tree is clean, so unwiring
    the AST binder from it changed nothing and the mutation survived: a guard is
    green over an empty corpus whether or not it works. Now the wiring has a
    case with a real violation in it.

    Returns `(violations, vars_checked)`; the count is the survivor floor's
    input.
    """
    # Union, not replacement. The regex still catches the
    # `os.path.dirname(os.path.dirname(...))` spelling the AST binder
    # deliberately leaves alone, and it keeps working on a file the AST cannot
    # parse. The AST adds the ternary and underscored-constant bindings the
    # regex cannot see. Measured 2026-08-29 over 430 files: 211 vars found by
    # both, 45 regex-only, 66 AST-only, and zero new violations once a
    # caller-supplied `workspace_root` was excluded from the AST producers.
    engine_vars = set(_ENGINE_BIND_RE.findall(text)) | _engine_bound_vars_ast(text)
    engine_vars.discard("")  # safety
    violations: list[str] = []
    for v in sorted(engine_vars):
        # \)? after the var name catches the `Path(BASE) / "outputs"` wrapper
        # form, not just the bare `BASE / "outputs"`.
        op = re.compile(r"\b" + re.escape(v) + r"\b\)?\s*(?:/|\+)\s*[\"']" + _DATA_DIRS + r"\b")
        join = re.compile(
            r"os\.path\.join\([^)]*\b" + re.escape(v) + r"\s*,\s*[\"']" + _DATA_DIRS + r"\b"
        )
        # v is PROVEN engine-bound here, so joinpath/f-string forms of the same
        # bypass are unambiguous, with no benign-param risk like the line-based
        # test guards against.
        jp = re.compile(r"\b" + re.escape(v) + r"\b\.joinpath\(\s*[\"']" + _DATA_DIRS + r"\b")
        fs = re.compile(r"\{[^{}]*\b" + re.escape(v) + r"\b[^{}]*\}/" + _DATA_DIRS + r"\b")
        for i, line in enumerate(text.splitlines(), 1):
            if op.search(line) or join.search(line) or jp.search(line) or fs.search(line):
                violations.append(f"{rel}:{i}: ({v} is engine-root-bound) {line.strip()}")
    return violations, len(engine_vars)


def _scan_roots() -> list[Path]:
    root = get_workspace_root()
    return [root / "scripts", root / ".claude"]


def _is_exempt(rel: str) -> bool:
    return any(s in rel for s in _EXEMPT_SUBSTRINGS)


def _nested_checkout_roots(base: Path) -> tuple[Path, ...]:
    """Directories under *base* that are separate git checkouts.

    A git worktree or submodule nested in the engine tree (Claude Code places
    them under `.claude/worktrees/`) is a checkout of this same repository, not
    engine source. Two reasons it must not be walked, and the second is the one
    that makes this a scope defect rather than a preference.

    Every file in it is a copy of a file this guard already scans in its own
    tree, so one real finding is reported twice, and a finding that belongs to
    the other checkout's BRANCH is reported as this tree's.

    And the copy is walked WIDER than the original. `_scan_roots` covers
    `scripts/` and `.claude/` only, so a worktree living under `.claude/` drags
    that checkout's entire `tests/` tree into a scan that deliberately excludes
    `tests/` here. Measured 2026-07-27 against a locked worktree: 22 violations
    across 16 files, every one of them a `tests/` file this guard does not scan
    in its own tree, and not one of them present in this tree.

    Skipping them loses no coverage. Git already excludes the worktree
    directory, and each checkout runs this same guard over itself.
    """
    return tuple(marker.parent for marker in base.rglob(".git"))


def _scan_files(base: Path, root: Path):
    """Yield `(path, relative posix path)` for every guarded `.py` file."""
    nested = _nested_checkout_roots(base)
    for py in base.rglob("*.py"):
        if any(py.is_relative_to(checkout) for checkout in nested):
            continue
        yield py, py.relative_to(root).as_posix()


def test_no_data_dir_joined_to_engine_root():
    root = get_workspace_root()
    violations: list[str] = []
    inspected = 0
    for base in _scan_roots():
        if not base.exists():
            continue
        for py, rel in _scan_files(base, root):
            if _is_exempt(rel):
                continue
            try:
                text = py.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            inspected += 1
            for i, line in enumerate(text.splitlines(), 1):
                if (_BYPASS.search(line) or _BYPASS_JOIN.search(line)
                        or _BYPASS_JOINPATH.search(line) or _BYPASS_FSTRING.search(line)):
                    violations.append(f"{rel}:{i}: {line.strip()}")
    # Survivor floor: an empty offender list only means "clean" if files actually
    # reached the line scan. Measured 427 files on 2026-08-26; floored well below
    # so retiring a script does not fail this test. If _scan_roots() stops
    # resolving, or _is_exempt() drifts to true for everything, the count collapses
    # and this fires instead of reporting a silent all-clear.
    assert inspected >= 256, f"only {inspected} files reached the bypass scan"
    assert not violations, (
        "Data directory joined directly to an engine root (bypasses the data-root "
        "seam -> misroutes private data into the engine clone). Use the matching "
        "get_*_dir() helper instead:\n  " + "\n  ".join(violations)
    )


def test_no_engine_root_alias_joined_to_data_dir():
    """Cross-line alias guard: catch `root = get_workspace_root()` (line A) then
    `root / "outputs"` (line B). The line-based _BYPASS regex misses this because
    `root` is ambiguous on its own line; this check first learns which local vars
    are bound to an engine-root producer, then flags any join of one to a data dir.
    """
    root = get_workspace_root()
    violations: list[str] = []
    inspected = 0
    for base in _scan_roots():
        if not base.exists():
            continue
        for py, rel in _scan_files(base, root):
            if _is_exempt(rel):
                continue
            try:
                text = py.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            # Union, not replacement. The regex still catches the
            # `os.path.dirname(os.path.dirname(...))` spelling the AST binder
            # deliberately leaves alone, and it keeps working on a file the AST
            # cannot parse. The AST adds the ternary and underscored-constant
            # bindings the regex cannot see. Measured 2026-08-29 over 430 files:
            # 211 vars found by both, 45 regex-only, 66 AST-only, and zero new
            # violations once a caller-supplied `workspace_root` was excluded
            # from the AST producers.
            found, checked = _bound_var_violations(text, rel)
            violations.extend(found)
            inspected += checked
    # Survivor floor: counts engine-bound vars that actually reached the per-line
    # regex checks. Measured 258 on 2026-08-26; floored well below so retiring a
    # script does not fail this test. If _is_exempt() drifts to true for every file,
    # or _ENGINE_BIND_RE stops matching any binding, the count collapses to zero and
    # this fires instead of reporting a silent all-clear.
    assert inspected >= 154, f"only {inspected} engine-bound vars were checked"
    assert not violations, (
        "Engine-root variable joined to a data dir (cross-line seam bypass). Bind the "
        "data path from the matching get_*_dir() helper instead:\n  " + "\n  ".join(violations)
    )


def test_a_nested_checkout_is_skipped_and_its_twin_in_the_tree_is_not(tmp_path):
    """Both halves of the nested-checkout skip, so it cannot widen into a hole.

    Half one: an identical bypass planted inside a nested git checkout is
    skipped. Half two: the same bypass planted in ordinary scan scope is still
    found. Delete the skip in `_scan_files` and the first assertion fails;
    widen it to swallow the ordinary file and the second does.
    """
    bypass = 'x = get_workspace_root() / "outputs" / name\n'

    ordinary = tmp_path / "scripts" / "real.py"
    ordinary.parent.mkdir(parents=True)
    ordinary.write_text(bypass, encoding="utf-8")

    nested = tmp_path / "scripts" / "wt" / "copy.py"
    nested.parent.mkdir(parents=True)
    nested.write_text(bypass, encoding="utf-8")
    # What `git worktree add` leaves behind: a FILE named .git, not a directory.
    (tmp_path / "scripts" / "wt" / ".git").write_text(
        "gitdir: /elsewhere/.git/worktrees/wt\n", encoding="utf-8")

    scanned = {rel for _, rel in _scan_files(tmp_path / "scripts", tmp_path)}

    assert "scripts/wt/copy.py" not in scanned
    assert "scripts/real.py" in scanned


def test_engine_root_alias_regex_detects_synthetic_bypass():
    """Positive regression for the guard's own detection logic: a crafted
    engine-root-alias bypass must be caught by _ENGINE_BIND_RE + the data-dir
    pattern. Keeps the guard itself regression-proof (no file I/O)."""
    snippet = "r = get_workspace_root()\n...\nx = r / 'outputs' / name\n"
    engine_vars = set(_ENGINE_BIND_RE.findall(snippet))
    assert "r" in engine_vars
    op = re.compile(r"\br\s*(?:/|\+)\s*[\"']" + _DATA_DIRS + r"\b")
    assert any(op.search(line) for line in snippet.splitlines())
    # And a data-root-bound var must NOT be collected as an engine producer.
    clean = "r = get_data_root()\nx = r / 'outputs'\n"
    assert "r" not in set(_ENGINE_BIND_RE.findall(clean))


def test_file_parent_idiom_detected_as_engine_producer():
    """Positive regression for finding #3 (2026-06-16): the bare __file__-parent
    idiom must be collected as an engine-root producer, and the `Path(VAR) /
    "datadir"` wrapper form must be caught by the op regex."""
    # os.path.dirname(os.path.dirname(...)) idiom
    snippet1 = (
        "BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\n"
        "OUTPUT = str(Path(BASE) / 'outputs' / 'x.docx')\n"
    )
    vars1 = set(_ENGINE_BIND_RE.findall(snippet1))
    assert "BASE" in vars1
    op = re.compile(r"\bBASE\b\)?\s*(?:/|\+)\s*[\"']" + _DATA_DIRS + r"\b")
    assert any(op.search(line) for line in snippet1.splitlines()), (
        "Path(BASE) / 'outputs' wrapper form must match the op regex"
    )

    # Path(__file__).resolve().parent.parent idiom, direct join
    snippet2 = (
        "base = Path(__file__).resolve().parent.parent\n"
        "p = str(base / 'outputs' / 'doc.md')\n"
    )
    vars2 = set(_ENGINE_BIND_RE.findall(snippet2))
    assert "base" in vars2
    op2 = re.compile(r"\bbase\b\)?\s*(?:/|\+)\s*[\"']" + _DATA_DIRS + r"\b")
    assert any(op2.search(line) for line in snippet2.splitlines())


def test_every_data_dir_this_guard_names_is_actually_caught():
    """One positive case per directory in `_DATA_DIRS`, in every join form.

    Written out rather than derived from `_DATA_DIRS`, deliberately. A test
    parametrized over the very alternation the defect shrinks deletes its own
    coverage: drop `auto-memory` from the pattern and a derived case list drops
    the `auto-memory` case with it, and the run stays green over a guard that
    stopped looking. This list has to be edited by hand when the pattern is,
    which is the point.
    """
    expected = ["threads", "crm", "outputs", "knowledge", "plans", "datastore",
                "context", "auto-memory", "chronicle", "admin", "personal",
                # The nine HEADING OS 2 family roots, written out here on
                # purpose even though `_DATA_DIRS` derives them: this list is
                # the anti-vacuity half, and deriving it from the pattern it
                # checks would delete its own coverage.
                "canon", "ledger", "status", "research", "work", "inbound",
                "build", "trace", "asset",
                # The C3 subject roots not already above, from the registry's
                # `subject_roots:` line (`knowledge` and `personal` are listed
                # first); written out for the same anti-vacuity reason.
                "accounts", "company", "content", "people", "inbox", "system",
                "archive"]
    missed = []
    for name in expected:
        if not _BYPASS.search(f'p = get_workspace_root() / "{name}" / "x.md"'):
            missed.append(f"{name} (operator form)")
        if not _BYPASS_JOIN.search(f'p = os.path.join(WORKSPACE_ROOT, "{name}", "x.md")'):
            missed.append(f"{name} (os.path.join form)")
        if not _BYPASS_JOINPATH.search(f'p = get_workspace_root().joinpath("{name}")'):
            missed.append(f"{name} (joinpath form)")
        if not _BYPASS_FSTRING.search(f'p = f"{{get_workspace_root()}}/{name}/x.md"'):
            missed.append(f"{name} (f-string form)")
    assert not missed, (
        "these data directories are named in _DATA_DIRS but a bypass spelled "
        "with them is not detected:\n  " + "\n  ".join(missed))

    # And the other direction, so this is not a pattern that matches everything:
    # an ENGINE directory joined to an engine root is ordinary correct code.
    for engine_dir in ("scripts", "config", "reference", "docs", "tests"):
        assert not _BYPASS.search(f'p = get_workspace_root() / "{engine_dir}"'), engine_dir
    # `build` and `work` are ordinary English words, so the two that entered
    # this pattern on 2026-09-19 need their own non-match cases. A literal that
    # merely BEGINS with one is not a family root.
    for innocent in ("builder", "buildings", "workspace", "workflows",
                     "statusline", "canonical", "traceback"):
        assert not _BYPASS.search(f'p = get_workspace_root() / "{innocent}"'), innocent
    # What this does NOT claim, measured rather than assumed on 2026-09-19: a
    # HYPHEN is a word boundary, so `"research-notes"` and `"work-in-progress"`
    # DO match, exactly as `"plans-of-mine"` has matched since this guard was
    # written. That is the pattern failing toward over-reporting, which is the
    # direction a seam guard should fail in, and it is pre-existing rather than
    # introduced here. Written down so the next reader finds it stated instead
    # of rediscovering it as a surprise.
    assert _BYPASS.search('p = get_workspace_root() / "research-notes"')
    assert _BYPASS.search('p = get_workspace_root() / "plans-of-mine"')


def test_the_family_roots_are_derived_and_all_nine_are_in_the_pattern():
    """The shared-root half: this guard's vocabulary comes from the same call
    its two siblings use, so a tenth family is inside all three on the day it is
    added rather than on the day someone remembers this file.

    Both directions. Every name `family_roots()` returns must be caught here,
    and the derivation must be a real one: a hand-typed copy would pass the
    first assertion and drift the moment `FAMILIES` changes, so the pattern is
    asked to contain the LIVE tuple rather than a list repeated here.
    """
    roots = family_roots()
    assert len(roots) == 9, roots
    # Derived, not copied: the LIVE tuple's own join has to be in the pattern.
    # A hand-typed list would satisfy the per-name loop below and still drift
    # the moment `FAMILIES` gains a family.
    assert _DATA_DIRS.endswith("|".join(roots) + ")"), _DATA_DIRS
    for name in roots:
        assert _BYPASS.search(f'p = get_workspace_root() / "{name}" / "x.md"'), name
        assert _BYPASS_JOIN.search(
            f'p = os.path.join(WORKSPACE_ROOT, "{name}", "x.md")'), name


def test_joinpath_and_fstring_direct_producer_forms_detected():
    """Positive regression (2026-06-28 sweep): the line-based guard must catch the
    joinpath() and f-string forms of a direct-producer bypass, which the operator
    and os.path.join regexes miss."""
    assert _BYPASS_JOINPATH.search('m = get_workspace_root().joinpath("outputs")')
    assert _BYPASS_FSTRING.search('p = f"{get_workspace_root()}/outputs/x.json"')
    # __file__-parent idiom in both forms
    assert _BYPASS_JOINPATH.search('Path(__file__).resolve().parent.parent.joinpath("crm")')
    # HOS-185, the same producer spelled `.parents[N]`; `[0]` is `scripts/`.
    assert _BYPASS.search('p = Path(__file__).resolve().parents[1] / "outputs"')
    assert not _BYPASS.search('p = Path(__file__).resolve().parents[0] / "outputs"')
    # A benign INJECTED param (not a producer expr) must NOT match the line-based
    # joinpath guard -- this is dead_letter.py's real shape.
    assert not _BYPASS_JOINPATH.search('outputs = workspace_root.joinpath("outputs")')
    # ...but a data-dir literal that isn't ours stays clean.
    assert not _BYPASS_JOINPATH.search('x = get_workspace_root().joinpath("scripts")')


def test_joinpath_and_fstring_alias_forms_detected_binding_aware():
    """Cross-line: an engine-bound alias var used via joinpath()/f-string must be
    caught, while the same var name used as a benign param default must not be
    collected as engine-bound in the first place."""
    snippet = "root = get_workspace_root()\nm = root.joinpath('outputs')\np = f'{root}/threads/t.md'\n"
    engine_vars = set(_ENGINE_BIND_RE.findall(snippet))
    assert "root" in engine_vars
    jp = re.compile(r"\broot\b\.joinpath\(\s*[\"']" + _DATA_DIRS + r"\b")
    fs = re.compile(r"\{[^{}]*\broot\b[^{}]*\}/" + _DATA_DIRS + r"\b")
    assert any(jp.search(line) for line in snippet.splitlines())
    assert any(fs.search(line) for line in snippet.splitlines())
    # Param default (= None) is not a producer -> var not collected -> joinpath safe.
    param = "def f(workspace_root=None):\n    o = workspace_root.joinpath('outputs')\n"
    assert "workspace_root" not in set(_ENGINE_BIND_RE.findall(param))


# ============================================================
# The AST binder, pinned against the shapes it exists for
# ============================================================
#
# Added 2026-08-29 after `scripts/utils/crm.py` joined a data directory to a
# module constant behind a ternary and the regex binder reported nothing. These
# run on literal snippets, never on the tree, so the guard stays regression-proof
# without file I/O.

def test_the_ast_binder_sees_a_producer_behind_a_ternary():
    """The exact line that got through, verbatim."""
    src = ('_ws_root = Path(workspace_root) if workspace_root else _WORKSPACE_ROOT\n'
           '_stages = parse_pipeline_stages(_ws_root / "context" / "pipeline.md")\n')
    assert "_ws_root" in _engine_bound_vars_ast(src)
    # And the reason a widened regex was not enough: the shipped one is blind to
    # it, so the AST binder is carrying this case alone.
    assert "_ws_root" not in set(_ENGINE_BIND_RE.findall(src))


def test_the_ast_binder_sees_an_underscored_module_constant():
    """`WORKSPACE_ROOT` cannot match starting at the `_` of `_WORKSPACE_ROOT`."""
    assert "p" in _engine_bound_vars_ast("p = _WORKSPACE_ROOT\n")
    assert "p" in _engine_bound_vars_ast("p = __WORKSPACE_ROOT\n")


def test_the_ast_binder_sees_the_file_parent_idiom_in_either_spelling():
    for src in ("BASE = Path(__file__).resolve().parent.parent\n",
                "BASE = Path(__file__).parent.parent\n",
                "BASE = override if override else Path(__file__).resolve().parent.parent\n",
                # HOS-185: the `.parents[N]` spelling, and the climb from a name
                # bound to `Path(__file__)`. MEASURED 2026-10-03: all four
                # returned set() before the binder walked `ast.Subscript`.
                "BASE = Path(__file__).resolve().parents[1]\n", "HERE = Path(__file__)\nBASE = HERE.parent.parent\n",
                "BASE = Path(__file__).parent.parents[0]\n", "HERE = Path(__file__).resolve()\nBASE = HERE.parents[2]\n"):
        assert "BASE" in _engine_bound_vars_ast(src), src


def test_the_ast_binder_spares_a_caller_supplied_root():
    """A parameter is the caller's choice, which is the sanctioned override.

    Real call sites follow exactly this pattern and must stay clean:
    `crm_autolog._address_book_dir` and `crm_autolog._contacts_dir`. An earlier
    version of this binder flagged both, and a third, the CRM aggregator
    removed on 2026-09-25, whose root was another executive's clone.
    """
    assert _engine_bound_vars_ast("ws = Path(workspace_root)\n") == set()
    assert _engine_bound_vars_ast("repo_path = clones[slug]\n") == set()
    assert _engine_bound_vars_ast("r = get_data_root()\n") == set()


def test_the_ast_binder_spares_someone_elses_parent_chain():
    """Shape alone is not enough; the argument decides whose tree it is."""
    assert _engine_bound_vars_ast("B = Path(other).resolve().parent.parent\n") == set()
    # One parent is `scripts/`, not the repository root.
    assert _engine_bound_vars_ast("B = Path(__file__).resolve().parent\n") == set()
    assert _engine_bound_vars_ast("H = Path(__file__)\nB = H.resolve().parents[0]\n") == set()


def test_the_ast_binder_degrades_to_the_regex_on_unparseable_source():
    """A syntax error must narrow the claim, never fail the suite.

    `.claude/` carries hook scripts that are edited by hand; one broken file
    must not turn this guard into a red run about something it does not govern.
    """
    assert _engine_bound_vars_ast("def (:\n") == set()


def test_the_ast_binder_is_not_matching_everything():
    """Anti-vacuity from the other side: a binder that returned every name would
    pass every test above and flood the tree with false positives."""
    src = ("a = 1\nb = get_data_root()\nc = some_call(x, y)\n"
           "d = {'k': 'v'}\ne = [i for i in range(3)]\n")
    assert _engine_bound_vars_ast(src) == set()


def test_the_ast_binder_sees_an_annotated_assignment():
    """`x: Path = _WORKSPACE_ROOT` is an `AnnAssign`, a different node type.

    Missed by the first version of the binder, and mutation-caught: skipping
    `AnnAssign` changed nothing until this case existed.
    """
    assert "p" in _engine_bound_vars_ast("p: Path = _WORKSPACE_ROOT\n")
    assert "p" in _engine_bound_vars_ast(
        "p: Path = override if override else _WORKSPACE_ROOT\n")
    assert _engine_bound_vars_ast("p: Path\n") == set()  # no value, no binding


def test_the_scan_flags_a_ternary_bound_var_end_to_end():
    """The wiring, not just the binder.

    This drives the SAME function the tree scan calls, on a file that really
    does contain the defect. The tree itself is clean, so without this case
    unwiring the AST binder from the scan changed nothing and the mutation
    survived: green over an empty corpus is not evidence.
    """
    src = ('_ws_root = Path(workspace_root) if workspace_root else _WORKSPACE_ROOT\n'
           '_stages = parse_pipeline_stages(_ws_root / "context" / "pipeline.md")\n')
    violations, checked = _bound_var_violations(src, "scripts/utils/crm.py")

    assert checked >= 1, "no engine-bound var reached the per-line check"
    assert len(violations) == 1, violations
    assert "crm.py:2" in violations[0]
    assert "_ws_root is engine-root-bound" in violations[0]
    found, _ = _bound_var_violations("H = Path(__file__).resolve()\nR = H.parents[1]\nx = R / 'crm'\n", "s.py")
    assert found == ["s.py:3: (R is engine-root-bound) x = R / 'crm'"], found


def test_the_scan_spares_the_fixed_form():
    """The shape that replaced it must be clean, or the guard blocks its own fix."""
    src = ('    if workspace_root:\n'
           '        _ws_root = Path(workspace_root)\n'
           '        _pipeline_file = _ws_root / "context" / "pipeline.md"\n'
           '    else:\n'
           '        _pipeline_file = get_context_dir() / "pipeline.md"\n')
    violations, _ = _bound_var_violations(src, "scripts/utils/crm.py")
    assert violations == [], violations


# ============================================================
# HOS-5 section 6: the count of hard-coded data addresses may only fall
# ============================================================
#
# Same guarantee as everything above, mechanised by a COUNT rather than by a
# pattern: engine code must not spell an address in the Data Repository, it must
# ask for one. The pattern half catches a literal joined to an ENGINE root. This
# half catches the other spelling, a literal joined to the DATA root, which is
# not a bypass and is not wrong today, and which HOS-5 section 6 requires to be
# unable to GROW while the conversion onto `data_path()` runs.
#
# It lives in this file and not a new one because the guarantee is this file's.
# `.claude/rules/development-standards.md` obligation 4, operator decision
# 2026-09-21: a second subject in a guarantee is a second test FUNCTION in the
# file that already holds it, not a new file.

#: The object name of `scripts/check-data-path-references.py` as it stood BEFORE
#: this gate existed, so the failing half below fails against that version and
#: not against a moving reference.
#:
#: An object name and never `HEAD`. While the work is uncommitted the two are
#: the same object and the failing half really does fail; the moment the branch
#: lands they are different objects, and a test pinned to `HEAD` then either
#: goes red on its own author or keeps passing over a tree that already carries
#: the fix, proving nothing. HOS-36 did the second of those on `main` on
#: 2026-09-21. Not abbreviated, either: an abbreviation is resolved against the
#: objects that exist at read time and can go ambiguous on a repository that
#: grows.
_PRE_PINS_GATE = "438d66670389d5fa2961014871f026150e2b2bda"  # pragma: allowlist secret


def _load_scanner(source: str, name: str, where: Path):
    """Load the reference scanner from source TEXT, current or historical.

    By text rather than by path, because the historical version has to be
    materialised somewhere and it must not be materialised INSIDE the worktree.
    An earlier yard extracted a tree into `.tmp/` here and the containment guard
    then resolved that copy as the session's own checkout, which made the real
    worktree read as a neighbour and refused every further tool call.

    `where` is the caller's `tmp_path`, and it is a parameter rather than a
    `tempfile` default for two reasons this suite already enforces. Its own
    teardown counts scratch that survives OUTSIDE pytest's managed tree against
    a frozen baseline of zero, and this helper failed that on its first run with
    two files left in `/tmp`; and `/tmp` is emptied on every boot and shared by
    every parallel yard on this machine, so nothing here may land there.

    The module under load inserts its own parent's parent on `sys.path` at
    import time, which from a scratch directory is a directory that has no
    business being there. The caller restores the snapshot; see the test.

    It is registered in `sys.modules` BEFORE `exec_module`, and the caller pops
    it. Not politeness: `@dataclass` reaches `sys.modules[cls.__module__]` while
    it is processing the class, so a module carrying one raises
    `AttributeError: 'NoneType' object has no attribute '__dict__'` at load
    rather than at use. MEASURED 2026-09-23 loading the pinned
    `scripts/utils/runtime_write_guard.py`, whose `Taint` is a frozen dataclass.
    The four callers that predate that one load modules with no dataclass in
    them, so this changes nothing for them.
    """
    path = where / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


#: The scanner as it stood before the prohibition, the count-ratchet version.
#: An object name and never `HEAD`, for the reason `_PRE_PINS_GATE` states.
_PRE_PINS_PROHIBITION = "1d40517d00c7f69675fe9f39d31061d3ed8a84fc"  # pragma: allowlist secret

#: A marked line whose literal joins a data root, the shape 196 sites had, and
#: an address assembled from segments, which no single literal spells.
_MARKED_SOURCE = 'P = root / "crm/contacts"  # leak-guard: ok\nQ = root / "crm" / "contacts"\n'


def _assert_the_pins_prohibition(root: Path, rel: str, tmp_path: Path) -> None:
    gate = _load_scanner((root / rel).read_text(encoding="utf-8"), "gate_now", tmp_path)
    assert hasattr(gate, "pins_verdict"), "no section 6 prohibition on this tree"
    (engine := tmp_path / "engine").joinpath("config").mkdir(parents=True)
    exemptions = engine / gate.PINS_EXEMPTIONS_REL
    text = 'X = root / "outputs/drafts"'
    finding = gate.PinFinding("scripts/x.py", 3, 3, text, ("outputs/drafts",))

    def verdict(entries) -> int:
        if entries is None:
            exemptions.unlink(missing_ok=True)
        else:
            exemptions.write_text(json.dumps({"entries": entries}), encoding="utf-8")
        return gate.pins_verdict(engine, [finding], as_json=False)

    covered = {"path": "scripts/x.py", "statement": text,
               "reason": "an invented statement for this test, a comparison key"}
    assert verdict([]) == 1                                   # uncovered
    assert verdict([covered]) == 0                            # exempted, with a reason
    assert verdict([covered, {**covered, "statement": "Y = 1"}]) == 1   # stale
    assert verdict([{**covered, "path": "scripts/other.py"}]) == 1      # wrong file
    assert verdict([{**covered, "reason": "a key"}]) == 2      # a fragment
    assert verdict(None) == 2                                  # no file at all

    # The marker no longer takes a literal out of the prohibition.
    pattern = re.compile(r"(?<![\w/.-])((?:crm)/[\w./-]*)")
    refs = gate.extract_python(_MARKED_SOURCE, "scripts/m.py", pattern)
    assert [(r.marked, r.assembled, r.pins, r.span) for r in refs] == [
        (True, False, True, (1, 1)), (False, True, True, (2, 2))], refs

    # -- the failing half -------------------------------------------------
    got = subprocess.run(["git", "cat-file", "-p", _PRE_PINS_PROHIBITION],
                         cwd=root, capture_output=True, text=True)
    assert got.returncode == 0, f"{_PRE_PINS_PROHIBITION} is not in this clone"
    old = _load_scanner(got.stdout, "gate_before", tmp_path)
    assert not hasattr(old, "pins_verdict")
    assert old.extract_python(_MARKED_SOURCE, "scripts/m.py", pattern) == []


# ============================================================
# HOS-5 phase 6: the instrument that rewrites those addresses must refuse
# a conversion that changes BEHAVIOUR
# ============================================================
#
# Same guarantee as everything above, one level up. The checks above hold that
# engine code does not spell a data address; `scripts/dev/data-path-equivalence.py`
# is what moves the spelled ones onto a registry name, so a defect in IT writes
# the bypass rather than catching it, and it shipped in the first slice with no
# test of any kind.
#
# The subject is the two refusals added in the second slice, and the measurement
# that forced the first of them. MEASURED 2026-09-21 on a clean tree at
# `21846fb2`, which already carried the first slice's hand revert of
# `scripts/context-floor-audit.py`: running the tool over `scripts/` and
# `.claude/` converted that site straight back. Its result is read inside
# `try: ... except Exception: memory = 0`, so the front door's refusal arrives
# as a silent zero and the audit row reads healthy. A refusal that lives only in
# a report is not a refusal the next run makes.
#
# The second is `DO_NOT_CONVERT`. `.claude/hooks/memory-reconcile.py` carried a
# paragraph saying in as many words that this conversion had been tried,
# measured against three red tests, and reverted; the tool converted it anyway,
# because it reads code and prose is invisible to it.

#: `scripts/dev/data-path-equivalence.py` as it stood BEFORE the two refusals,
#: so the failing half fails against that version rather than against a moving
#: reference. An object name and never `HEAD`, for the reason `_PRE_PINS_GATE`
#: above states in full.
_PRE_REFUSAL_TOOL = "2f684ddb5a7ad784db64b5e05592d5d7e995addf"  # pragma: allowlist secret

#: One site, five surroundings. Only the first may be converted onto a front
#: door that RAISES, because only the first leaves the failure visible.
_SWALLOW_CASES = (
    ("a handler that assigns a default",
     "try:\n    x = get_data_root() / 'a'\nexcept Exception:\n    x = 0\n", True),
    ("a handler that cannot catch a RuntimeError",
     "try:\n    x = get_data_root() / 'a'\nexcept OSError:\n    x = 0\n", False),
    ("a handler that re-raises",
     "try:\n    x = get_data_root() / 'a'\nexcept Exception:\n    raise\n", False),
    ("no try at all",
     "x = get_data_root() / 'a'\n", False),
    ("the site is IN the handler, so nothing guards it",
     "try:\n    pass\nexcept Exception:\n    x = get_data_root() / 'a'\n", False),
    # A bare `except:` catches everything, including the front door's refusal.
    # Its own case, because with only the named-exception rows a tool that
    # dropped the bare-handler branch entirely stayed green: MEASURED
    # 2026-09-21 by planting exactly that mutation.
    ("a bare except, which catches everything",
     "try:\n    x = get_data_root() / 'a'\nexcept:\n    x = 0\n", True),
)


def _swallow_verdict(tool, source: str) -> bool:
    """Drive the tool's own predicate over `source`, at its `get_data_root` call."""
    tree = ast.parse(source)
    parents = tool._outermost_join(tree)
    call = next(n for n in ast.walk(tree)
                if isinstance(n, ast.Call) and tool._callee(n) == "get_data_root")
    return tool._failure_is_swallowed(call, parents)


def test_the_conversion_tool_refuses_a_swallowed_failure_and_a_written_prohibition(tmp_path):
    """Both halves, against the pinned pre-refusal object.

    The passing half: this tree's tool refuses a site whose failure a handler
    would turn into a default, spares the four surroundings that leave the
    failure visible, and skips a whole file that forbids the conversion in its
    own words, quoting the line that forbids it.

    The failing half: the object pinned above carries neither refusal, and
    driving its own `convert_file` over a file carrying the marker returns no
    refusal at all. Point this test at that version and it goes red.
    """
    root = get_workspace_root()
    rel = "scripts/dev/data-path-equivalence.py"
    before_path = sys.path[:]
    try:
        _assert_the_conversion_tool_refuses(root, rel, tmp_path)
    finally:
        sys.path[:] = before_path


def _assert_the_conversion_tool_refuses(root: Path, rel: str, tmp_path: Path) -> None:
    tool = _load_scanner((root / rel).read_text(encoding="utf-8"), "eqv_now", tmp_path)

    # -- the passing half, refusal 4 --------------------------------------
    assert len(_SWALLOW_CASES) == 6, "the case table shrank; a table of one passes"
    for label, source, expected in _SWALLOW_CASES:
        assert _swallow_verdict(tool, source) is expected, label

    # -- the passing half, refusal 5 --------------------------------------
    # Driven through `convert_file`, which is what the CLI calls, and asserted
    # on what it RETURNS rather than on a restatement of its condition.
    marked = tmp_path / "marked.py"
    marked.write_text(f"# {tool.DO_NOT_CONVERT} -- measured, and reverted\n"
                      "from scripts.utils.workspace import get_data_root\n"
                      "x = get_data_root() / 'auto-memory'\n", encoding="utf-8")
    done, skipped = tool.convert_file(tmp_path, "marked.py", {}, {})
    assert done == [] and len(skipped) == 1, (done, skipped)
    assert "do-not-convert" in skipped[0] and "marked.py" in skipped[0], skipped[0]
    assert marked.read_text(encoding="utf-8").count("get_data_root") == 2, \
        "the file was rewritten despite forbidding it"

    # SCOPE, since 2026-09-22. A marker inside a function refuses that function
    # and leaves its neighbours to the five rules. The blanket form above is
    # what a module-level marker still does, and both shapes are asserted here
    # because narrowing a refusal is the direction that can silently stop
    # refusing. MEASURED on `scripts/workspace-health.py`, which carries eight
    # seam sites and one marker: the previous version answered with ONE blanket
    # refusal naming the file, this one answers with eight, seven of them from
    # the rules.
    # NESTED, so the same case pins innermost-wins. A mutation taking the
    # OUTERMOST enclosing function survived the flat fixture this replaced,
    # because with one function the two readings agree; here they do not, and
    # the outermost reading refuses the neighbour too.
    scoped = tmp_path / "scoped.py"
    scoped.write_text("from scripts.utils.workspace import get_data_root\n"
                      "def outer():\n"
                      "    def forbidden():\n"
                      f"        # {tool.DO_NOT_CONVERT} -- measured, reverted\n"
                      "        return get_data_root() / 'auto-memory'\n"
                      "    return get_data_root() / 'auto-memory'\n",
                      encoding="utf-8")
    done, skipped = tool.convert_file(tmp_path, "scoped.py", {}, {})
    assert done == [] and len(skipped) == 1, (done, skipped)
    assert "forbidden()" in skipped[0] and "scoped.py:5" in skipped[0], skipped[0]

    # The honest case still passes through: an identical file WITHOUT the marker
    # is not refused for this reason. A rule that refuses every file satisfies
    # every refusal test.
    plain = tmp_path / "plain.py"
    plain.write_text(marked.read_text(encoding="utf-8").split("\n", 1)[1],
                     encoding="utf-8")
    assert tool.convert_file(tmp_path, "plain.py", {}, {}) == ([], [])

    # -- the failing half --------------------------------------------------
    got = subprocess.run(["git", "cat-file", "-p", _PRE_REFUSAL_TOOL],
                         cwd=root, capture_output=True, text=True)
    if got.returncode != 0:
        import pytest
        pytest.skip(f"{_PRE_REFUSAL_TOOL} is not in this clone")
    old = _load_scanner(got.stdout, "eqv_before", tmp_path)
    assert not hasattr(old, "DO_NOT_CONVERT"), (
        "the pinned pre-refusal object already carries the marker, so this test "
        "proves nothing about what changed; re-pin it")
    assert not hasattr(old, "_failure_is_swallowed")
    assert old.convert_file(tmp_path, "marked.py", {}, {}) == ([], []), (
        "the pinned object refused the written prohibition, which it could not do")


# ============================================================
# HOS-75: a helper that ACCEPTS a data root must answer under THAT root
# ============================================================
#
# The same guarantee once more, from the caller's side. Everything above holds
# that engine code does not spell a data address; this holds that when a caller
# names the tree, the named tree is the one that answers. A function that takes
# a `data_root`, forwards it to a relocation step, and builds its BASE from a
# helper that takes no root resolves under whatever the environment names. The
# caller believes it is isolated and is not, which is failure B: code that
# thinks it is acting on a throwaway tree acting on the live overlay.
#
# MEASURED 2026-09-22 on two throwaway directories inside a worktree, with
# `HEADING_OS_DATA` pointed at the first and the second supplied as the
# argument: `calibration_dir(data_root=<second>)` answered
# `<first>/datastore/operations/memory-calibration`, and the second appeared
# nowhere in it. Nothing raised, and the same call with no argument answered
# identically, so the parameter was indistinguishable from working.
#
# The sweep that found it is `scripts/dev/data-root-honoured.py`, written before
# the fix because a hand search finds only the shape you already pictured. It
# asks the AST, never the text, for the reason this suite gives elsewhere: a fix
# that quotes the bad expression to explain it reddens a substring scan, and the
# docstring of the repaired function quotes it three times.

#: `scripts/utils/calibration_store.py` as it stood BEFORE the parameter was
#: removed. A blob object and never `HEAD`, so the failing half fails against
#: that version rather than against a moving reference.
_PRE_FIX_CALIBRATION_STORE = "c523e056c718e6b6a55a2400e88fe5f8c9fad516"  # pragma: allowlist secret

#: Floors under the sweep, MEASURED 2026-09-22 at 545 files and 50 helpers. A
#: scan whose corpus collapses reports a clean tree, which is the one way this
#: check can pass while establishing nothing. Helper floor 40 -> 39 -> 37 -> 36
#: on 2026-09-25, MEASURED: the blind set fell 41 -> 39 -> 37 -> 36 by exactly the
#: helpers HOS-5 section 6 deleted (get_outputs_dir, get_plans_dir;
#: get_crm_config_path, get_people_file) and then section 7 (private_cache_dir).
#: Helper floor 36 -> 23 on 2026-09-25, MEASURED by running the sweep on this
#: branch and on its base: the blind set fell 36 -> 23 by exactly the thirteen
#: helpers HOS-130 deleted with the executive model (get_admin_slugs,
#: get_all_active_exec_slugs, get_ceo_only_references, get_ceo_only_scripts,
#: get_exec_data_root, get_shared_knowledge_dir, load_admin_config,
#: load_business_registry, load_exec_registry, load_fleet, load_github_org,
#: repo_name_for, validate_admin), and no helper joined it.
#: Helper floor 23 -> 21 on 2026-09-25, MEASURED the same way: the blind set fell
#: 23 -> 21 by exactly the two root helpers HOS-132 deleted (get_corporate_root,
#: get_personal_root), which were counted only because they were sweep seeds.
_SWEEP_FILE_FLOOR = 450
_SWEEP_HELPER_FLOOR = 21


def _load_honoured_tool():
    """The sweep, by its real path: it derives the engine root from `__file__`."""
    path = get_workspace_root() / "scripts" / "dev" / "data-root-honoured.py"
    spec = importlib.util.spec_from_file_location("_data_root_honoured", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_HONOURED_PROBE = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from scripts.utils.calibration_store import calibration_dir, current_path
print(calibration_dir())
print(current_path())
for fn in (calibration_dir, current_path):
    try:
        fn(data_root=Path("/nowhere"))
    except TypeError as exc:
        print("REFUSED", exc)
"""


def test_a_helper_that_takes_a_data_root_answers_under_that_root(tmp_path):
    """Both halves, and the sweep that holds the rest of the tree.

    The failing half: the pinned pre-fix object carries the shape, and the sweep
    reports it `direct` there. Point the sweep at a tree where that object is
    the live file and it goes red, which is what it did before the fix.

    The passing half: no file under `scripts/` or `.claude/` carries the shape
    now, measured over a corpus with a floor under it; the sweep still refuses
    the shape and still spares the three honest spellings of a default, driven
    through its own entry point; and the repaired module answers under the root
    the environment names and refuses the argument it cannot honour.
    """
    root = get_workspace_root()
    tool = _load_honoured_tool()

    # -- the failing half ---------------------------------------------------
    got = subprocess.run(["git", "cat-file", "-p", _PRE_FIX_CALIBRATION_STORE],
                         cwd=root, capture_output=True, text=True)
    if got.returncode != 0:
        import pytest
        pytest.skip(f"{_PRE_FIX_CALIBRATION_STORE} is not in this clone")
    before = tmp_path / "calibration_store_before.py"
    before.write_text(got.stdout, encoding="utf-8")
    # `get_datastore_dir` was deleted on 2026-10-16 (F-5.4.4); the pre-fix
    # file still calls it, so it is named here as the blind helper it was.
    blind = tool.base_blind_helpers() | {"get_datastore_dir"}
    old = tool.scan_file(before, blind)
    assert [(f.func, f.base, f.klass) for f in old] == [
        ("calibration_dir", "get_datastore_dir", "direct")], old

    # -- the passing half, the tree -----------------------------------------
    swept = tool.scan(["scripts", ".claude"])
    assert swept.files >= _SWEEP_FILE_FLOOR, swept.files
    assert len(swept.blind) >= _SWEEP_HELPER_FLOOR, sorted(swept.blind)
    assert [f for f in swept.findings if f.klass == "direct"] == []

    # -- the passing half, the sweep's own two directions --------------------
    probe = subprocess.run([sys.executable, str(root / "scripts" / "dev"
                                                / "data-root-honoured.py"),
                            "--self-test"],
                           cwd=root, capture_output=True, text=True)
    assert probe.returncode == 0, probe.stdout + probe.stderr

    # -- the passing half, the repaired module -------------------------------
    # A throwaway root named through the environment, never the operator's.
    named = tmp_path / "named-root"
    named.mkdir()
    env = dict(os.environ, HEADING_OS_DATA=str(named))
    env.pop("WORKSPACE_ROOT", None)
    ran = subprocess.run([sys.executable, "-c", _HONOURED_PROBE, str(root)],
                         cwd=root, env=env, capture_output=True, text=True)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    lines = [ln for ln in ran.stdout.splitlines() if ln.strip()]
    assert lines[0].startswith(str(named)), lines
    assert lines[1].startswith(str(named)), lines
    assert sum(1 for ln in lines if ln.startswith("REFUSED")) == 2, lines


#: The fixture engine tree's routing map. `docs/` routes ENGINE here on purpose:
#: it is what makes the overlay's own rule the only thing that can bring
#: `docs/kept-private/` into the scanner's scope, so the count below moves when
#: the overlay half of the map goes missing and not for any other reason.
_FIXTURE_ENGINE_MAP = """\
default: engine
data_default: private
rules:
  scripts/: engine
  docs/: engine
"""

#: The fixture OVERLAY's list of legacy data addresses (HOS-232; until then a
#: clamp in the overlay's half of the routing map). One entry, over a first
#: segment both trees have, so the overlay-only clause cannot rescue it.
_FIXTURE_LEGACY_ADDRESSES = "docs/kept-private/\n"

#: What the fixture engine tree says. Three references the overlay rule brings
#: into scope, and one under an overlay-only family that is in scope either way
#: -- the last is the floor: without it a run that lost the overlay map would
#: scan an empty corpus and the two counts could agree at zero.
_FIXTURE_ENGINE_SOURCE = '''\
"""A fixture module that names some overlay addresses."""
KEPT = "docs/kept-private/one.md"
ALSO = "docs/kept-private/two.md"
AND = "docs/kept-private/three.md"
FAMILY = "ledgerfx/four.md"
'''


def _clean_git_env(**extra):
    """The parent environment with every GIT_* variable removed, plus `extra`.

    NOT a convenience, and this file is the one that proved it. MEASURED
    2026-09-25: run under `git commit`, these two tests wrote `core.bare` into
    the ENGINE CLONE'S OWN `.git/config` and still reported passed. git exports
    `GIT_DIR` and `GIT_INDEX_FILE` to every hook it runs, the pre-commit hook
    named `HEADING OS engine/data guards` runs this file, and the `git init`
    below inherited that `GIT_DIR` and re-initialised the repository the commit
    was happening in. Every worktree of it then answered `fatal: this operation
    must be run in a work tree` until the key was removed by hand, twice. The
    same inheritance sends the scanner's own `git ls-files` at the wrong
    repository, so a run under the hook would have counted the engine's files
    and called it the fixture's.

    `scripts/canopus_check.py::git_child_env` is the scrub and its docstring
    carries the reasoning: a blanket PREFIX and never a list of the variables
    somebody thought of. Reused rather than copied.
    """
    env = dict(git_child_env(), **extra)
    # Goes red if a later edit hands `os.environ` to one of these children
    # again, which is the shape that caused the corruption.
    leaked = sorted(k for k in env if k.startswith("GIT_"))
    assert not leaked, leaked
    return env


def _fixture_trees(tmp_path):
    """An engine clone and an overlay, both small enough to build per test.

    Cheap on purpose: five files, one `git init`, no network and none of the
    operator's own tree. The scanner needs a git INDEX (it reads
    `git ls-files`), a routing map, and at least one file under one of its
    subtrees; that is the whole list.
    """
    engine = tmp_path / "engine"
    (engine / "config").mkdir(parents=True)
    (engine / "scripts").mkdir()
    (engine / "docs").mkdir()
    (engine / "config" / "routing-map.yaml").write_text(
        _FIXTURE_ENGINE_MAP, encoding="utf-8")
    (engine / "scripts" / "names_some_addresses.py").write_text(
        _FIXTURE_ENGINE_SOURCE, encoding="utf-8")
    for cmd in (["init", "-q"], ["add", "-A"]):
        subprocess.run(["git", *cmd], cwd=engine, check=True,
                       env=_clean_git_env(), capture_output=True, text=True)

    overlay = tmp_path / "overlay"
    (overlay / "config").mkdir(parents=True)
    (overlay / "config" / "legacy-data-addresses.txt").write_text(
        _FIXTURE_LEGACY_ADDRESSES, encoding="utf-8")
    # An overlay-only family, so the anchor pattern is never empty and the
    # scanner never refuses instead of counting.
    (overlay / "ledgerfx").mkdir()
    (overlay / "docs" / "kept-private").mkdir(parents=True)

    decoy = tmp_path / "decoy"
    (decoy / "config").mkdir(parents=True)
    return engine, overlay, decoy


def _scan_counts(engine, overlay, data_root_env):
    """Run the real gate over the fixture and return its two published counts."""
    env = _clean_git_env(HEADING_OS_DATA=str(data_root_env),
                         WORKSPACE_ROOT=str(engine), NO_COLOR="1")
    ran = subprocess.run(
        [sys.executable,
         str(get_workspace_root() / "scripts" / "check-data-path-references.py"),
         "--data-root", str(overlay), "--engine-root", str(engine), "--json"],
        cwd=engine, env=env, capture_output=True, text=True, timeout=300)
    # 0 is "everything resolved" and 1 is "something did not". Either is a run
    # that counted; 2 is a refusal, and a refusal is not a count.
    assert ran.returncode in (0, 1), ran.stdout + ran.stderr
    report = json.loads(ran.stdout)
    return report["corpus"]["references_in_scope"], len(report["new"])


def test_the_data_path_gate_counts_the_same_under_two_data_root_variables(tmp_path):
    """The gate's number is a property of its arguments, not of the environment.

    THE DEFECT, MEASURED 2026-09-24 in a worktree against the operator's own
    overlay. `scripts/check-data-path-references.py` was run twice with
    byte-identical `--data-root` and `--engine-root`, differing only in
    `HEADING_OS_DATA`, and reported 2867 references in scope against 2093, 140
    gated unresolved against 81, and 72 gated pins against 42. Both runs exited
    0, so the ratchet that gates the push accepted the smaller number in
    silence, and the `data_root` field printed in the report read the ARGUMENT
    in both. After the fix, the same two runs print 2867 / 140 / 72 each.

    Since HOS-232 the overlay half of the map is gone and the scope input
    that took its place, the overlay's list of legacy data addresses, is read
    from the ARGUMENT; the fixture carries it where the clamp used to be.

    HOS-115 attributed this to the corporate root and the families manifest;
    that was wrong, and so was the narrower attribution to the tree inference in
    `routing_tree_for`. Isolated the same day by running the scan three ways
    under the lying variable, the whole of the difference is the RULE SET:
    `load_routing_map` reads the overlay's own `config/routing-map-private.yaml`
    through `get_data_root()`, which is 208 of the map's 404 rules on that
    workspace, and every one of them CLOSES a path. Making only the tree
    inference honour the passed root moved none of the three numbers, because
    since the 2026-09-21 inversion both tree defaults are `private`.

    BOTH DIRECTIONS. The failing half is the assertion below run against the
    tree as it stood before the fix: the second call loses the fixture
    overlay's one clamp and counts fewer references, exactly as the real gate
    did. The passing half is the floor under it, which is what keeps this from
    agreeing at zero over a corpus that collapsed.
    """
    engine, overlay, decoy = _fixture_trees(tmp_path)

    honest_scope, honest_gated = _scan_counts(engine, overlay, overlay)
    lying_scope, lying_gated = _scan_counts(engine, overlay, decoy)

    # The floor: the honest run must have found the three references the overlay
    # rule governs plus the one under the overlay-only family.
    assert honest_scope >= 4, honest_scope
    assert honest_gated >= 1, honest_gated

    assert (honest_scope, honest_gated) == (lying_scope, lying_gated), (
        f"the gate counted {honest_scope}/{honest_gated} with HEADING_OS_DATA "
        f"at the overlay it was passed and {lying_scope}/{lying_gated} with it "
        f"pointed elsewhere, from identical arguments")


def test_a_root_without_an_overlay_map_is_refused_rather_than_read_open(tmp_path):
    """A supplied root may only ever CLOSE, and absence is how it would open.

    The other half of the same change, and the reason it is a separate
    assertion: losing the overlay's scope input shrinks what the gate checks.
    Until HOS-232 that input was the overlay's routing rules; since then it is
    `config/legacy-data-addresses.txt`, and a root without it is a refusal
    (exit 2), which says "I established nothing" instead of reporting a clean
    scan over a scope the argument did not choose.

    Asked of the gate's own entry point, never of the resolver in isolation, so
    the observable consequence is the exit code a caller sees.
    """
    engine, overlay, decoy = _fixture_trees(tmp_path)
    env = _clean_git_env(HEADING_OS_DATA=str(overlay),
                         WORKSPACE_ROOT=str(engine), NO_COLOR="1")
    ran = subprocess.run(
        [sys.executable,
         str(get_workspace_root() / "scripts" / "check-data-path-references.py"),
         "--data-root", str(decoy), "--engine-root", str(engine)],
        cwd=engine, env=env, capture_output=True, text=True, timeout=300)
    assert ran.returncode == 2, (ran.returncode, ran.stdout + ran.stderr)
    assert "legacy-data-addresses.txt" in (ran.stdout + ran.stderr), ran.stderr

    # The honest direction, so this cannot be satisfied by a gate that refuses
    # everything: the same command against the real overlay still counts.
    ok = subprocess.run(
        [sys.executable,
         str(get_workspace_root() / "scripts" / "check-data-path-references.py"),
         "--data-root", str(overlay), "--engine-root", str(engine), "--json"],
        cwd=engine, env=env, capture_output=True, text=True, timeout=300)
    assert ok.returncode in (0, 1), ok.stdout + ok.stderr


# ============================================================
# HOS-95: an IMPORT must not move the data root under the whole process
# ============================================================
#
# The same guarantee this file holds, asked of the one event that cannot be
# refused: `import`. `.claude/skills/flux-image/scripts/generate_image.py` called
# `load_env()` at MODULE level, against a root derived from its own `__file__`.
# `load_env` writes every key of that `.env` into `os.environ` with
# `setdefault`, and one of those keys is the data root, so importing the module
# repointed the data root of the ENTIRE PROCESS and every resolver called
# afterwards -- in any module, not only that one -- answered about the tree that
# checkout's `.env` names. Nothing printed a word about it.
#
# This is the shape the workspace already has a written rule against, in the
# form of a copied `.env` silently repointing every guard at the wrong tree.
# That one needs a human to copy a file. This one needs nobody, and it fires in
# any process the module reaches. What it costs is not one wrong path: any
# measurement taken in a contaminated process is suspect, and nothing
# distinguishes such a run from a clean one in its output. It was found that
# way, as a defect of another task's probe, where one child process out of many
# had been contaminated and the run looked entirely normal.
#
# MEASURED 2026-09-23 in a child with `HEADING_OS_DATA` unset and
# `WORKSPACE_ROOT` pointed at a fixture whose sibling overlay was the fixture's
# own: `get_data_root()` answered inside the fixture before the import and
# outside it after, with `HEADING_OS_DATA` going from unset to set by the import
# alone. Four modules carried the shape and all four are repaired.
#
# The sweep that holds the rest of the tree is `scripts/dev/import-env-purity.py`,
# an AST walk for the same reason its sibling above gives: a substring scan for
# `load_env` goes red on every fix that quotes the call to explain it, and the
# four repaired files quote it nine times between them.

#: `.claude/skills/flux-image/scripts/generate_image.py` as it stood BEFORE the
#: module-level `load_env` was removed. A blob object and never `HEAD`, so the
#: failing half fails against that version rather than a moving reference.
_PRE_FIX_GENERATE_IMAGE = "fae3c07b1d57fbc68e65120412ba08c53c56eab4"  # pragma: allowlist secret

#: Floors under the purity sweep, MEASURED 2026-09-23 at 566 files parsed and 45
#: env-writing helpers derived over `scripts/`, `.claude/` and `tests/`. A scan
#: whose corpus collapses reports a clean tree, which is the one way this check
#: can pass while establishing nothing.
_PURITY_FILE_FLOOR = 450
_PURITY_HELPER_FLOOR = 30


def _load_purity_tool():
    """The sweep, by its real path: it derives the engine root from `__file__`."""
    path = get_workspace_root() / "scripts" / "dev" / "import-env-purity.py"
    spec = importlib.util.spec_from_file_location("_import_env_purity", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: Asks the resolver, imports the module by its REAL path, asks again. Driven in
#: a child because the answer is a property of a process, and an in-process
#: version would contaminate the suite with the very thing it measures.
_PURITY_PROBE = """
import importlib.util, json, os, sys
engine, target = sys.argv[1], sys.argv[2]
sys.path.insert(0, engine)
from scripts.utils.paths import get_data_root
out = {"env_before": os.environ.get("HEADING_OS_DATA"),
       "root_before": str(get_data_root())}
spec = importlib.util.spec_from_file_location("_probe_target", target)
spec.loader.exec_module(importlib.util.module_from_spec(spec))
out["env_after"] = os.environ.get("HEADING_OS_DATA")
out["root_after"] = str(get_data_root())
print(json.dumps(out))
"""


def test_importing_an_engine_module_does_not_move_the_data_root(tmp_path):
    """Both halves, and the sweep that holds the rest of the tree.

    The failing half: the pinned pre-fix blob carries the shape, and the sweep
    reports it at the `load_env` line. That is the file this test would go red
    against if the call ever came back.

    The passing half: importing the repaired module in a child leaves both the
    variable and the resolved root exactly where they were; no file under
    `.claude/` carries the shape now, over a corpus with a floor under it; and
    the sweep still refuses the shape and still spares a call moved into a
    function, driven through its own entry point.
    """
    root = get_workspace_root()
    tool = _load_purity_tool()
    helpers = tool.env_writing_helpers()

    # -- the failing half ---------------------------------------------------
    got = subprocess.run(["git", "cat-file", "-p", _PRE_FIX_GENERATE_IMAGE],
                         cwd=root, capture_output=True, text=True)
    if got.returncode != 0:
        import pytest
        pytest.skip(f"{_PRE_FIX_GENERATE_IMAGE} is not in this clone")
    old = tool.findings_in(ast.parse(got.stdout), helpers,
                           Path("generate_image_before.py"), got.stdout,
                           deferred=tool.env_deferring_helpers())
    assert [f.kind for f in old] == ["load_env"], old
    assert "load_env(" in old[0].source, old[0].source

    # -- the passing half, the live module in a child ------------------------
    # A fixture workspace with its own sibling overlay, named through the
    # environment. The operator's real root is never pointed at: it is removed
    # from the child's environment outright, which is also what makes the
    # contamination visible if it returns.
    clone = tmp_path / "warren-engine"
    overlay = tmp_path / ".heading-os-data"
    clone.mkdir()
    overlay.mkdir()
    env = dict(os.environ, WORKSPACE_ROOT=str(clone))
    env.pop("HEADING_OS_DATA", None)
    target = root / ".claude" / "skills" / "flux-image" / "scripts" / "generate_image.py"
    ran = subprocess.run([sys.executable, "-c", _PURITY_PROBE, str(root),
                          str(target)],
                         cwd=root, env=env, capture_output=True, text=True)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    seen = json.loads(ran.stdout.strip().splitlines()[-1])
    assert seen["env_before"] is None, seen
    assert seen["env_after"] is None, seen
    assert seen["root_before"] == str(overlay.resolve()), seen
    assert seen["root_after"] == seen["root_before"], seen

    # -- the passing half, the tree -----------------------------------------
    swept = tool.scan(["scripts", ".claude", "tests"])
    assert swept.files >= _PURITY_FILE_FLOOR, swept.files
    assert len(swept.helpers) >= _PURITY_HELPER_FLOOR, sorted(swept.helpers)
    assert [f for f in swept.findings
            if str(f.path).startswith(str(root / ".claude"))] == []

    # -- the passing half, the sweep's own two directions --------------------
    probe = subprocess.run([sys.executable, str(root / "scripts" / "dev"
                                                / "import-env-purity.py"),
                            "--self-test"],
                           cwd=root, capture_output=True, text=True)
    assert probe.returncode == 0, probe.stdout + probe.stderr


#: The four modules that bind `_clock = make_run_clock()` at module level.
_CLOCK_FACTORY_SITES = ("crm-health", "generate-crm-dashboard",
                        "generate-dashboard", "knowledge-health")


def test_a_closure_factory_bound_at_import_is_not_an_env_load(tmp_path):
    """HOS-172: the sweep judges a helper by what runs when it is CALLED.

    Before the fix the sweep walked every nested `def` of a helper as if the
    helper ran it, so `make_run_clock()`, which only returns a `_clock` that
    loads `.env` on first use, read as an import-time load. MEASURED 2026-09-28:
    four findings `_clock = make_run_clock()`, one per site above, carried by
    four exemptions, while importing each module in a child changed no variable
    of `os.environ` at all.

    The honest half: importing a site leaves the environment where it was, and
    the sweep reports none of the four lines. The refusing half: calling the
    returned closure at import, directly or through the bound name, and a real
    import-time load through a called helper chain, are each still one finding.
    """
    root = get_workspace_root()
    tool = _load_purity_tool()
    swept = tool.scan(["scripts"])

    # -- the honest half ------------------------------------------------------
    clone = tmp_path / "warren-engine"
    clone.mkdir()
    (tmp_path / ".heading-os-data").mkdir()
    env = dict(os.environ, WORKSPACE_ROOT=str(clone))
    env.pop("HEADING_OS_DATA", None)
    ran = subprocess.run([sys.executable, "-c", _PURITY_PROBE, str(root),
                          str(root / "scripts" / "crm-health.py")],
                         cwd=root, env=env, capture_output=True, text=True)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    seen = json.loads(ran.stdout.strip().splitlines()[-1])
    assert seen["env_after"] is None and seen["root_after"] == seen["root_before"]
    flagged = {f.path.stem for f in swept.findings
               if "make_run_clock" in f.source}
    assert flagged == set(), flagged
    assert set(_CLOCK_FACTORY_SITES) <= {p.stem for p in
                                         (root / "scripts").glob("*.py")
                                         if "_clock = make_run_clock()"
                                         in p.read_text(encoding="utf-8")}

    # -- the refusing half ----------------------------------------------------
    imp = ("from scripts.utils.workspace import make_run_clock, "
           "get_default_tz\n")
    for body in ("make_run_clock()()\n",
                 "_clock = make_run_clock()\n_clock()\n",
                 "TZ = get_default_tz()\n"):
        src = imp + body
        got = tool.findings_in(ast.parse(src), set(swept.helpers),
                               Path("some-module.py"), src,
                               deferred=swept.deferred)
        assert len(got) == 1, (body, got)


# ============================================================
# HOS-5: the runtime write guard forgives only sites that exist
# ============================================================
#
# Same guarantee as everything above, from the write side. `RECORD_EXEMPTIONS`
# in `scripts/utils/runtime_write_guard.py` is the list of overlay writes the
# guard sees, spells as junk, and deliberately passes. Its own documented rule
# is that a row matching NO write site is stale, and the reason that rule exists
# is not tidiness: the needle is matched by `str.endswith` on the file and
# `in` on the chain, so a row whose writer has moved keeps a loaded weapon
# aimed at whatever spelling comes to occupy that address next.
#
# It lives in this file and not a new one because the guarantee is this file's:
# engine code does not reach a data address by hand. `.claude/rules/
# development-standards.md` obligation 4, operator decision 2026-09-21.

#: `scripts/utils/runtime_write_guard.py` as it stood BEFORE the 2026-09-23
#: sweep of the two dead rows, so the failing half fails against that version
#: and not against a moving reference. An object name and never `HEAD`, for the
#: reason `_PRE_PINS_GATE` sets out above.
_PRE_DEAD_ROW_SWEEP = "3c76a6b106a60f780922628cf4e2c66f55658fd6"  # pragma: allowlist secret

#: MEASURED 2026-09-23 on this checkout: `scan()` reports 443 rooted write
#: sites and `RECORD_EXEMPTIONS` carries 11 rows. Both are floors and not pins.
#: A floor rather than an equality because the scan walks a DISCOVERED corpus,
#: and the assertions below that matter are inside a loop over it: over an empty
#: corpus every row is stale and every row is live at once, and a loop asserts
#: nothing either way. The numbers are deliberately below the measurement so an
#: honest new writer does not redden this, and far enough above zero that a
#: collapsed walk does.
#:
#: `_EXEMPTION_ROWS_FLOOR` 10 -> 9, MEASURED 2026-09-24 on this checkout: 443
#: rooted write sites and 9 rows. The margin the 2026-09-23 figure left was one
#: row, and two legitimate removals spent it the same day: `scripts/
#: aggregate-crm.py :: crm/aggregated/...` took the table to 10 without this
#: number moving, and the HOS-122 removal of `scripts/scrutinize-replay.py ::
#: scrutiny/_human_agreement_` took it to 9. Both went for the same reason and
#: it is the one this table calls stale rather than optional: their writers
#: moved onto the front door, `_SEAM_LEGACY_ARG` has no legacy argument to read
#: at such a call, and a needle spelling a legacy DIRECTORY then matches
#: nothing. So the floor has to FALL as HOS-5 section 6 proceeds, and each fall
#: is a measurement recorded here rather than a threshold negotiated to fit a
#: commit. What it still refuses is unchanged: an emptied table, which is what
#: would satisfy the staleness assertion below by having nothing in it.
#: Lowered on the operator's word typed in this yard, 2026-09-24, the brief
#: having forbidden it and the blocker having been reported to him first.
#:
#: 9 -> 8, MEASURED 2026-09-25: 443 rooted write sites, 0 offending, 8 rows,
#: 0 stale. `scripts/utils/scrutinize_record.py :: scrutiny/runs.jsonl` went
#: for the same reason, when HOS-5 section 6 deleted `moved_file` and
#: `record_path()` moved onto the front door. Stands on the operator's word
#: at the commit that carries it, as the fall before it did.
#:
#: `_SITES_FLOOR` 400 -> 390, MEASURED 2026-09-28 on this tree: the scan finds
#: 390 rooted write sites. The drop is files leaving, not a walk collapsing:
#: HOS-166 removed eleven skills, their scripts and the migration mover, and
#: their write sites went with them. The floor is set AT the measured count and
#: stays a floor; a collapsed walk still falls far below it.
_SITES_FLOOR = 390
_EXEMPTION_ROWS_FLOOR = 8


def _guard_tree() -> Path:
    """The checkout this test file sits in.

    Derived from `__file__` and from nothing else. Not `get_workspace_root()`,
    which reads `WORKSPACE_ROOT` from the environment and, in a copied `.env`,
    silently names the main clone; not the common git directory, which a
    worktree shares with it. A guard asserted against a tree other than the one
    under test reports clean for the same reason a guard pointed at the wrong
    tree does, and the two are indistinguishable by their result.
    """
    return Path(__file__).resolve().parent.parent


def _plant(where: Path, rel: str, first: str, second: str) -> Path:
    site = where / rel
    site.parent.mkdir(parents=True, exist_ok=True)
    site.write_text(
        "from scripts.utils.workspace import get_data_root\n"
        "\n"
        "\n"
        "def dump(payload):\n"
        f"    path = get_data_root() / '{first}' / '{second}'\n"
        "    path.write_text(payload)\n",
        encoding="utf-8")
    return site


def _plant_two_junk_writes(where: Path) -> tuple[Path, list[Path]]:
    """Two writers the table must refuse, each for a different reason.

    The first is `scripts/update-manager.py` spelling `updates/state.json`,
    rooted at `get_data_root()` and therefore unsanctioned: the exact shape the
    row removed on 2026-09-23 matched, by file suffix AND chain substring. It is
    planted rather than described so the pinned table and this one are asked the
    same question about the same bytes.

    The second is an IMPOSTOR: a file no row names, carrying a needle a row that
    is still live does name. It exists because a row is two conditions and a
    mutation can drop either one. MEASURED 2026-09-23, without this file the
    mutation that deletes the `endswith` on the file and keeps only the needle
    SURVIVED: every live row still matched its own site, so nothing read as
    stale, and no needle in the table happened to match `updates/state.json`, so
    the first writer was still refused. The table had stopped being scoped to
    files at all and every assertion stayed green.
    """
    return where, [
        _plant(where, "scripts/update-manager.py", "updates", "state.json"),
        _plant(where, "scripts/impostor.py", "scrutiny", "runs.jsonl"),
    ]


def test_no_exemption_row_forgives_a_write_site_that_does_not_exist(tmp_path):
    """Both halves of the 2026-09-23 dead-row sweep, against the pinned version.

    The passing half, and it is two claims that have to hold together. Every row
    in this tree's table still matches at least one real write site, so nothing
    is forgiving an address nothing writes. And the guard still REFUSES: a
    planted writer spelling `updates/state.json` under `scripts/update-manager.py`
    comes back offending, which is what a table with no row for it is supposed
    to do and what no count of rows can establish.

    The failing half. The pinned version's table carries three rows matching no
    site on this tree, and it FORGIVES the planted writer, reporting nothing
    offending over the same bytes. Point the first assertion at that version and
    it fails; point the third at it and it fails. MEASURED 2026-09-23: 13 rows
    then and 11 now, 2 stale then and 0 now. RE-MEASURED 2026-09-24, after HOS-5
    § 6 moved `crm.py::aggregated_home` onto the front door and retired the row
    standing on its legacy spelling: 13 rows then and 10 now, 3 stale then and 0
    now. RE-MEASURED AGAIN the same day, after HOS-122 moved
    `scrutinize-replay.py::sheet_home` the same way: 13 rows then and 9 now, 4
    stale then and 0 now. HOS-166, 2026-09-28: the mover `scripts/heading_os_2/move.py`
    was retired, so its flock-file row left the live table and joins `dead`.

    THE `dead` LIST GROWS WITH EVERY SUCH MOVE, and that is the shape of this
    test rather than churn in it. Each entry is a row whose writer left the seam
    for the front door, where `_SEAM_LEGACY_ARG` has no legacy argument to read,
    so a needle spelling a legacy DIRECTORY matches nothing. The list is ordered
    by the pinned table, never by hand, because that is the order
    `stale_exemptions` walks.
    """
    root = _guard_tree()
    from scripts.utils import runtime_write_guard as guard

    real = guard.scan(root=root)
    assert len(real) >= _SITES_FLOOR, (
        f"the scan found {len(real)} rooted write sites under {root}, below the "
        f"floor of {_SITES_FLOOR}; the walk has collapsed and the per-row "
        "assertions below are running over an empty corpus")
    assert len(guard.RECORD_EXEMPTIONS) >= _EXEMPTION_ROWS_FLOOR, (
        f"{len(guard.RECORD_EXEMPTIONS)} exemption rows, below the floor of "
        f"{_EXEMPTION_ROWS_FLOOR}; an emptied table satisfies the staleness "
        "assertion below by having nothing in it")

    # The guard's own matcher answers this, never a copy of it written here: a
    # test carrying its own `endswith` + `in` passes while the real one has
    # stopped agreeing with it.
    assert guard.stale_exemptions(real) == [], (
        "these rows forgive no write site that exists; the writer moved and the "
        "row stayed, so it now forgives whatever comes to occupy that address: "
        f"{guard.stale_exemptions(real)}")

    # -- the guard still refuses, on the very address a row used to forgive ---
    base, files = _plant_two_junk_writes(tmp_path / "planted")
    planted = guard.scan(root=base, files=files, resolvers=frozenset())
    assert len(planted) == len(files), [s.describe() for s in planted]
    for site in planted:
        assert guard.is_runtime_spelling(site.chain), site.describe()
    assert [s.describe() for s in guard.offending(planted)] == \
        [s.describe() for s in planted], (
            "a planted junk write was not refused: either the table still "
            "carries a row for an address nothing in this tree writes, or a "
            "row has stopped being scoped to the file it names")

    # -- the failing half ----------------------------------------------------
    got = subprocess.run(["git", "cat-file", "-p", _PRE_DEAD_ROW_SWEEP],
                         cwd=root, capture_output=True, text=True)
    if got.returncode != 0:
        import pytest
        pytest.skip(f"{_PRE_DEAD_ROW_SWEEP} is not in this clone")

    before_path = sys.path[:]
    try:
        # The pinned module imports `runtime_dir.STORE_DIRNAME` at load, a
        # constant HOS-5 F1 (2026-09-25) replaced with the registry's
        # `runtime_store:` key, so the import becomes the value it then held.
        # The historical spelling lives only in this historical text.
        old = _load_scanner(got.stdout.replace("from scripts.utils.runtime_dir import STORE_DIRNAME", 'STORE_DIRNAME = "runtime-junk"'), "runtime_write_guard_before", tmp_path)
        assert len(old.RECORD_EXEMPTIONS) > len(guard.RECORD_EXEMPTIONS), (
            "the pinned object already carries the sweep, so this test proves "
            "nothing about what changed; re-pin it")
        # THIS tree's matcher over the PINNED table, so the two tables are
        # judged over one corpus. Asking the old module would compare two walks
        # and a difference could then be either table or tree.
        dead = [("scripts/update-manager.py", "updates/state.json"), ("scripts/council-record-verdict.py", "council/_verdicts.jsonl"),
                ("scripts/scrutinize-replay.py", "scrutiny/_human_agreement_"),
                ("scripts/utils/scrutinize_record.py", "scrutiny/runs.jsonl"),
                ("scripts/aggregate-crm.py", "crm/aggregated/audit/aggregation-log.jsonl"),
                ("scripts/heading_os_2/move.py", "_manifest/.migration-process-owner")]
        assert (stale := guard.stale_exemptions(real, old.RECORD_EXEMPTIONS)) == dead, stale
        # And the dead row was not inert: the pinned version FORGIVES the
        # writer it named, and refuses only the impostor. Selected by file and
        # never by index, because `scan` sorts by `(file, line)` and `impostor`
        # sorts before `update-manager`.
        by_file = {site.file: site for site in planted}
        assert old.exemption_for(by_file["scripts/update-manager.py"]) is not None, \
            "the pinned version did not forgive the planted write, so the " \
            "contrast this test is about does not exist"
        assert [s.describe() for s in old.offending(planted)] == \
            [by_file["scripts/impostor.py"].describe()], \
            [s.describe() for s in old.offending(planted)]
    finally:
        sys.modules.pop("runtime_write_guard_before", None)
        sys.path[:] = before_path


# ---------------------------------------------------------------------------
# HOS-113: the cross-kind fan-out moved from a tree walk to the manifest.
#
# `scripts/heading_os_2/kinds.py::homes_under` replaced `moved_homes` at ten
# sites on 2026-09-24. `moved_homes` is still here, deliberately, and these two
# tests are half the reason: while both exist the manifest-derived answer can be
# held against the tree-derived one, and once the seam goes that becomes
# impossible to write. The other half is the reconciliation guard below, which
# exists for the same window.
# ---------------------------------------------------------------------------

#: The object name of `scripts/workspace-health.py` as it stood BEFORE the
#: reconciliation guard landed. An object name and never `HEAD`, for the reason
#: `_PRE_PINS_GATE` above gives: while the work is uncommitted the two are the
#: same object and the failing half really does fail, and the moment the branch
#: lands they are different objects and a `HEAD`-pinned test proves nothing.
_PRE_HOME_RECONCILIATION = "463a1209eada26040200c643e7ecc95b81419f45"  # pragma: allowlist secret

#: Floor under the prefix loop in the equivalence test. A test whose assertions
#: all sit inside a loop over a discovered corpus passes over an empty one.
_FANOUT_PREFIX_FLOOR = 4


def _ho2_fixture(root: Path, rows: list[tuple[str, str, str]],
                 dirs: list[str]) -> None:
    """A data root with a HEADING OS 2 per-file manifest and some homes on disk.

    `rows` is `(legacy path, family, leaf)` per record; `dirs` are directories to
    create under `root`. `tests/ho2_manifest.py` built this until the 2026-09-21
    test-suite split took it, so it is rebuilt here rather than imported.

    `family-locations.tsv` is written with its header ONLY. That is not laziness:
    `resolve.manifest_dir` decides a tree HAS a manifest by probing for that
    file, so it has to exist, while `check_agreement` -- the one thing that would
    demand its rows agree with the per-file table -- is not on the code path
    either `kinds_under` or `moved_homes` takes. A fixture that invented rows for
    it would be asserting against a table nothing under test reads.
    """
    from scripts.heading_os_2 import resolve

    manifest = root / resolve.PERMANENT_MANIFEST_DIR
    manifest.mkdir(parents=True, exist_ok=True)
    (manifest / resolve.LOCATIONS_FILENAME).write_text(
        "family\tleaf\tcurrent_dir\ttarget_dir\tstate\tfiles\tspread_over_dirs\n",
        encoding="utf-8")
    body = "".join(f"{path}\t{family}\t{leaf}\tmove\tceo-only\t{family}/{leaf}\n"
                   for path, family, leaf in rows)
    (manifest / resolve.PER_FILE_FILENAME).write_text(
        "path\tfamily\tleaf\tmethod\tsensitivity\ttarget\n" + body,
        encoding="utf-8")
    for rel in dirs:
        (root / rel).mkdir(parents=True, exist_ok=True)


def test_the_manifest_fanout_answers_what_the_tree_walk_answers(tmp_path):
    """`homes_under` answers what `moved_homes` answered, tuple for tuple, in order.

    `moved_homes` was DELETED 2026-09-25 with the rest of the seam (HOS-5
    section 6), so the tree walk it did is no longer callable. The tuples below
    are what it answered on this fixture, MEASURED that day by running the
    deleted version against it: equal to `homes_under` at four prefixes of four.

    The conversion of ten sites on 2026-09-24 is a conversion rather than a
    behaviour change only while this holds, so it is asserted rather than
    argued. Equality is on the TUPLE and not on a sorted copy of it, which is
    the half the design document that proposed this change could not check: its
    own equivalence script compared `tuple(sorted(...))` on both sides, and
    re-derived without the sorting on 2026-09-24 the SETS agreed at nine
    prefixes of nine while the ORDER disagreed at two, `datastore` and
    `outputs`. `_in_incumbent_order` exists because of that measurement.

    The floor case is the one every converted caller depends on and the one a
    public clone, a fresh overlay and every scratch fixture actually runs: with
    no manifest and no family roots the answer is exactly `(legacy,)`, so a
    converted caller scans the one directory it scanned before the move existed.
    """
    import pytest

    from scripts.heading_os_2.kinds import homes_under, kinds_under

    root = tmp_path / "overlay"
    root.mkdir()
    # INVENTED prefix names, never the operator's real legacy spellings. Two
    # reasons and both are rules. This repository is public, so a fixture
    # carrying the real tree's vocabulary ships it to everyone. And
    # `scripts/check-legacy-outputs-sites.py` counts a hard-coded legacy address
    # in engine Python against a ceiling; a fixture spelling four of them raises
    # that ceiling by four to test a mechanism that does not care what the
    # directories are called. What the fan-out needs is a tail several kinds
    # share, which `wide` and `split` supply, beside two that only one kind
    # takes.
    #
    # `split` is the awkward one on purpose: `trace/chronicle` holds it, so the
    # home is `trace/chronicle/split` and the leaf name is not the tail. That is
    # the shape `moved_home`'s own docstring records the write seam getting
    # wrong.
    _ho2_fixture(
        root,
        rows=[("wide/people.md", "status", "entity"),
              ("wide/pipeline.md", "status", "matter"),
              ("wide/strategy.md", "canon", "position"),
              ("split/a.md", "trace", "report"),
              ("split/b.md", "work", "draft"),
              ("split/c.md", "asset", "image"),
              # DECLARED AND NOT ON DISK: `ledger/interaction/split` is not in
              # `dirs` below, because this is the commonest shape on a real tree
              # and the one a fixture is most likely to omit. MEASURED
              # 2026-09-24 on the operator's overlay, TEN homes are in this
              # state, every one of them a kind that has not flipped yet. Both
              # functions must leave it out: `moved_homes` never sees it because
              # it walks the disk, and `homes_under` must drop it on its own
              # `is_dir()`. Without this row that filter is dead code no
              # assertion reaches, and a mutation removing it survives.
              ("split/d.md", "ledger", "interaction"),
              ("lone/p.md", "build", "spec"),
              ("same/c.md", "trace", "chronicle")],
        dirs=["wide", "status/entity/wide", "status/matter/wide",
              "canon/position/wide", "split", "trace/report/split",
              "work/draft/split", "asset/image/split", "build/spec/lone",
              "trace/chronicle/same"])

    prefixes = ("wide", "split", "lone", "same")
    assert len(prefixes) >= _FANOUT_PREFIX_FLOOR, prefixes
    walk = {"wide": "wide canon/position/wide status/entity/wide status/matter/wide",
            "split": "split work/draft/split trace/report/split asset/image/split",
            "lone": "build/spec/lone", "same": "trace/chronicle/same"}
    homes_seen = 0
    for prefix in prefixes:
        walked = tuple(root / rel for rel in walk[prefix].split())
        derived = homes_under(root / prefix, data_root=root)
        assert derived == walked, (f"{prefix}: manifest {[str(p) for p in derived]}"
                                   f" != tree walk {[str(p) for p in walked]}")
        homes_seen += len(walked)
    # Outside the loop, so a fixture that built nothing cannot pass this.
    assert homes_seen >= 9, homes_seen

    # The floor: no manifest, no family roots.
    bare = tmp_path / "bare"
    bare.mkdir()
    for prefix in prefixes:
        legacy = bare / prefix
        assert homes_under(legacy, data_root=bare) == (legacy,), prefix
    (bare / "wide").mkdir()
    assert homes_under(bare / "wide", data_root=bare) == (bare / "wide",)
    # The data root itself, and a path that is not under it at all.
    assert homes_under(bare, data_root=bare) == (bare,)
    outside = tmp_path / "elsewhere"
    assert homes_under(outside, data_root=bare) == (outside,)

    # A manifest that is PRESENT and MALFORMED, which is the one case where
    # `homes_under` deliberately departs from the `kinds_under` it is built on.
    # That function returns `()` for an ABSENT table and propagates every other
    # manifest error; this one degrades to `(legacy,)` for all of them and logs
    # what it swallowed, matching the incumbent. Operator-side ruling
    # 2026-09-24: the loudness a refusal would buy is imaginary, because two of
    # the ten converted callers are hooks catching bare `Exception`, so a raise
    # reaches a handler that turns it into a default anyway. Asserted here
    # because the ruling is the whole reason the `except` clause exists, and an
    # unasserted `except` is one a later reader deletes as over-caution.
    broken = tmp_path / "broken"
    broken.mkdir()
    _ho2_fixture(broken, rows=[("wide/x.md", "status", "entity")],
                 dirs=["wide", "status/entity/wide"])
    from scripts.heading_os_2 import resolve
    (broken / resolve.PERMANENT_MANIFEST_DIR
     / resolve.PER_FILE_FILENAME).write_bytes(b"\xff\xfe not utf-8 at all")
    resolve.invalidate_cache()
    # The named refusal rather than a blind `Exception`: this asserts that the
    # table really is unreadable and that `kinds_under` really does propagate,
    # which is the premise the degradation below is a departure FROM.
    with pytest.raises(resolve.ManifestMalformed):
        kinds_under("wide", data_root=broken)
    assert homes_under(broken / "wide", data_root=broken) == (
        broken / "wide",), (
        "a present-and-malformed table did not degrade to the legacy "
        "directory; it refuses where the incumbent answered")


def test_the_home_reconciliation_reports_a_tree_the_manifest_does_not_declare(
        tmp_path, monkeypatch):
    """Both halves of the HOS-113 reconciliation guard.

    The passing half is three claims. On a tree where every home on disk is
    declared, the section reports no issue. On the same tree with ONE directory
    planted that no row declares -- which is exactly the shape the manifest
    lagging the tree produces, MEASURED three days behind on 2026-09-24 -- the
    section REFUSES, and the home it names is the planted one. And on a tree
    with no family roots and no manifest it reports NOTHING rather than
    reporting every declared home as missing, because that tree is a public
    clone, a fresh overlay and every scratch fixture.

    The failing half: the pinned object above is `scripts/workspace-health.py`
    as it stood before this landed, and it carries no reconciliation at all, so
    there was no mechanism by which a home nobody declared could be reported. It
    is read with `ast` rather than executed, because the question asked of it is
    whether the function and its section row exist, and executing a 1800-line
    script to find out is a worse way to learn it.

    The guard REPORTS and resolves nothing, by the operator's decision of
    2026-09-24, so the last claim is that the planted directory is still there,
    unmoved and unregistered, after the section has run.
    """
    health = _load_health_module(tmp_path)
    try:
        root = tmp_path / "overlay"
        root.mkdir()
        # The autouse pin in `tests/conftest.py` points `HEADING_OS_DATA` at
        # `tmp_path` itself; the section resolves `get_data_root()` at call time,
        # so it has to be moved onto the fixture root rather than its parent.
        monkeypatch.setenv("HEADING_OS_DATA", str(root))
        _ho2_fixture(
            root,
            rows=[("wide/people.md", "status", "entity"),
                  ("wide/pipeline.md", "status", "matter")],
            dirs=["wide", "status/entity/wide", "status/matter/wide"])

        # -- the passing half, the honest tree --------------------------------
        assert health.check_home_reconciliation() == 0, (
            "the section reported an issue on a tree whose every home is "
            "declared; it refuses the honest case")

        # -- the passing half, the case it exists to catch --------------------
        planted = root / "canon" / "reference" / "wide"
        planted.mkdir(parents=True)
        issues = health.check_home_reconciliation()
        assert issues == 1, (
            f"a directory carrying the `wide` tail was planted under a leaf "
            f"no row declares, and the section reported {issues} issue(s)")
        assert planted.is_dir(), (
            "the reconciliation moved or removed the planted directory; it is "
            "required to report and resolve nothing")
        assert not (root / "_manifest" / "families.tsv").read_text(
            encoding="utf-8").count("canon\treference"), (
            "the reconciliation taught the manifest what the tree held, which "
            "is the one thing it must never do")

        # -- the passing half, a prefix that is not top-level ------------------
        # The version of this section that shipped for an hour on 2026-09-24
        # walked only `parts[0]` of each row, and an adversarial read found the
        # blind spot: a kind declared under a prefix's top-level ANCESTOR but
        # not under the prefix itself read as clean. Three of the nine prefixes
        # the ten converted sites consume are not top-level, and one of them is
        # the Action Queue's, where a missed home means a second copy of the
        # store started beside the live one.
        deep = tmp_path / "deep"
        deep.mkdir()
        monkeypatch.setenv("HEADING_OS_DATA", str(deep))
        _ho2_fixture(
            deep,
            # `trace/report` is declared under `wide/` AND under the deeper
            # `wide/inner/queue`. `trace/scratch` is declared under `wide/` only,
            # so a top-level-only check finds its home declared and stops.
            rows=[("wide/inner/queue/a.md", "trace", "report"),
                  ("wide/elsewhere/b.md", "trace", "scratch")],
            dirs=["wide", "trace/report/wide/inner/queue",
                  "trace/scratch/wide/elsewhere",
                  "trace/scratch/wide/inner/queue"])
        issues = health.check_home_reconciliation()
        assert issues == 1, (
            f"a home was planted at a THREE-SEGMENT prefix that no kind "
            f"declares there, while its top-level ancestor IS declared, and "
            f"the section reported {issues} issue(s)")

        # -- the passing half, the two floors ----------------------------------
        bare = tmp_path / "bare"
        bare.mkdir()
        monkeypatch.setenv("HEADING_OS_DATA", str(bare))
        assert health.check_home_reconciliation() == 0, (
            "a tree with no family roots and no manifest reported an issue")

        # Family roots and NO manifest is the other floor, and it is a FINDING
        # rather than a clean run: the tree has fanned out and nothing declares
        # it, so `homes_under` answers with the legacy directory alone while the
        # incumbent tree walk finds every home. MEASURED 2026-09-24 on this
        # shape: three homes from `moved_homes`, one from `homes_under`. A
        # section that printed OK here would be certifying the one tree where
        # the conversion is not equivalent.
        undeclared_tree = tmp_path / "undeclared"
        (undeclared_tree / "status" / "entity" / "wide").mkdir(parents=True)
        (undeclared_tree / "wide").mkdir()
        monkeypatch.setenv("HEADING_OS_DATA", str(undeclared_tree))
        assert health.check_home_reconciliation() == 1, (
            "a tree carrying family roots with no manifest reported clean; "
            "that is the shape where the fan-out silently loses homes")

        # -- the failing half --------------------------------------------------
        before = subprocess.run(
            ["git", "cat-file", "-p", _PRE_HOME_RECONCILIATION],
            cwd=get_workspace_root(), capture_output=True, text=True)
        if before.returncode != 0:
            import pytest
            pytest.skip(f"{_PRE_HOME_RECONCILIATION} is not in this clone")
        old = ast.parse(before.stdout)
        defined = {n.name for n in ast.walk(old)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert "check_home_reconciliation" not in defined, (
            "the pinned pre-guard object already carries the reconciliation, so "
            "this test proves nothing about what changed; re-pin it")
        assert "homes" not in {c.value for c in ast.walk(old)
                               if isinstance(c, ast.Constant)
                               and isinstance(c.value, str)}, (
            "the pinned object already knows the section name")
    finally:
        sys.modules.pop("workspace_health_under_test", None)


def _load_health_module(where: Path):
    """`scripts/workspace-health.py` loaded by path, for a test that drives it.

    By path because the filename is hyphenated and so is not an importable
    module name. Registered in `sys.modules` before `exec_module` and popped by
    the caller, for the reason `_load_scanner` above records.

    `where` is unused today and is kept in the signature deliberately: every
    other loader in this file takes the caller's `tmp_path` rather than reaching
    for `tempfile`, because `/tmp` is emptied on every boot and shared by every
    parallel yard on this machine.
    """
    path = get_workspace_root() / "scripts" / "workspace-health.py"
    spec = importlib.util.spec_from_file_location(
        "workspace_health_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["workspace_health_under_test"] = module
    before_path = sys.path[:]
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = before_path
    return module

