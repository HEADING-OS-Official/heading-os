#!/usr/bin/env python3
"""Does a skill, or engine code, name a data-repository address by its physical path?

    python scripts/audit-skill-data-paths.py            # human report, every finding
    python scripts/audit-skill-data-paths.py --check    # the gate: exit 1 on a rise, a fall, a stale entry
    python scripts/audit-skill-data-paths.py --json     # machine-readable
    python scripts/audit-skill-data-paths.py --engine [--check|--json]   # the same over engine code
    python scripts/audit-skill-data-paths.py --data-config [--check|--json]   # the data repo's config files

WHY. A move of the data repository's records changes one file, the data
repository's `config/data-paths.yaml`, and no skill. A skill that spells a
physical address (`status/entity/crm/contacts/`, `work/deliverable/outputs/documents/...`) (leak-guard: ok, scanner vocabulary)
breaks at the next move: `.claude/hooks/data-path-redirect.py` rewrites the
relative path into the data repository, where a moved folder no longer holds
the records, so a read finds nothing and a write recreates the old folder. The
way in is a logical name, resolved by `python scripts/data-path.py path <name>`
or `data_path()`.

`scripts/audit-skill-bash-paths.py` asks a narrower question, a bare data path
handed to a script inside a SKILL bash fence, and reports 0. This asks it of
every line of every text file under `.claude/skills/<skill>/`: SKILL.md body,
its YAML frontmatter, `references/`, and the rest.

## What a finding is

A path segment that starts with a current family root (canon, ledger, status,
research, work, inbound, build, trace, asset) or a legacy data directory
(outputs, datastore, crm, threads, knowledge, plans, auto-memory, context),
followed by `/` and at least one more character, and NOT preceded by a word
character, `:`, `/`, `.`, `$` or `-`. So `record:work/draft`,
`runtime:x/cache`, `scripts/outputs/`, `$ROOT/outputs/...` and `.claude/...`
are not findings, and a tail under a resolved directory
(`$(... path record:work/draft)/x`) is not either. The definition is the one
the 2026-10-12 skill best-practices scan measured with; it is lexical on
purpose, so it reads the same to every reader.

## What is exempt, and nothing else is

A reviewed entry in `config/skill-data-paths-exemptions.json`, in the format
`scripts/utils/statement_exemptions.py` defines for every gate here: the file,
the full text of the line the finding sits on, and a reason of at least eight
words. One entry covers ONE finding; two findings on one line need two entries.
An entry matching nothing is stale and refuses. There is no heuristic for
"illustrative": an example path, a caution that quotes the wrong address, and a
live instruction all look the same to this scanner, and only a reviewer's entry
tells them apart.

## The ratchet

`ceiling` in the same file is the count of findings no entry covers. A rise
refuses (a new physical path nobody judged); a fall refuses too, until the
ceiling is lowered to match, so the recorded number is always the tree's. The
ceiling may only go down.

Wired as a push ratchet (`scripts/utils/push_ratchets.py`) and not at
pre-commit, for the reason `legacy_outputs_ratchet` in `scripts/push-all.py`
gives.

## `--engine`: the same question over engine code (HOS-234, 2026-10-13)

Operator decision 2026-10-13, translated: "I want the whole system to work
this way, and everything new we build to follow this architecture, NO
hardcoded paths." The skills were walled; scripts, hooks and engine config were
not, and `scripts/check-data-path-references.py` only asks whether a named path
RESOLVES, never whether it should have been named at all.

`--engine` scans every text file git tracks or would carry (untracked, not
ignored) under `scripts/`, `.claude/hooks/` and `config/`,
whatever its suffix, because a suffix list is a list of holes; a file holding a
NUL byte is binary and skipped. The finding is the same lexical one as above,
plus one carve-out the skills scan does not need: a line carrying
`leak-guard: ok`, the marker `scripts/leak-guard.py` already reads as a
reviewed site, is not a finding, and neither is a bare `<family>/<leaf>` that
`families.is_valid` accepts (the `("trace", "report")` pair written with a
slash): that is the name of a record kind, the one `record:<family>/<leaf>`
asked for, used to say which kind is meant; `<family>/<leaf>/x` is an address
under it and is a finding. Two kinds of file are not scanned, named in (leak-guard: ok, scanner vocabulary)
REGISTRY_FILES and EXEMPTION_LEDGERS: the engine's default registry, which is
the address book and holds addresses by definition, and every gate's
`config/*-exemptions.json`, which quotes findings verbatim. `tests/` sits
outside these roots.

Its ledger is `config/engine-data-paths-exemptions.json`, same format, same
rule: the ceiling may only fall, and the goal is 0. It is a separate push
ratchet, `engine-data-paths`, in `scripts/utils/push_ratchets.py`.

Two more shapes, `--engine` only, added 2026-10-13 after the conversion
yards found them by eye. An address behind a placeholder for the data root
(`DATA_ROOT_PLACEHOLDER`), which the look-behind above passes because a `/`
precedes the root; a placeholder naming a registry entry (`<document-output>/x`)
is not one. And in Python, an address assembled from separate string segments
by `/`, `os.path.join`, `joinpath` or a `Path(...)` constructor: the AST is
asked for each outermost join, and a run of two or more constant segments
whose join the lexical pattern matches from its start is a finding (`root /
"crm" / "contacts"`). What it still does NOT see: a segment held in a variable
or built by an f-string, a root written alone (`root / "crm"`), and a join in
any file but Python. The first is the `--pins-check` half of
`scripts/check-data-path-references.py`.

One more, `--engine` and Python only, added 2026-10-13 (HOS-234 part 7): a
single segment joined onto a root that is ITSELF a registry name, where the
segment is a child that has a registry name of its own (`data_path("chronicle")
/ "personal"` when `chronicle-personal` exists). The root counts as named when
it is a resolver call with a constant name, a same-file zero-argument function
whose one `return` is such a call, or a local variable assigned once from
either in the same function. The children are read from the engine's default
registry, `scripts/data-names.example.yaml`, so the verdict is the same on
every clone. A root built any other way (a `get_*_dir()` helper, an argument)
is not seen; a looser test that matched the segment alone found 28 joins, most
of them file names under a directory nobody had named, which is noise.

## `--data-config`: the data repository's own config files (HOS-234 part 5)

The same question asked of the config files INSIDE the data repository, which
the engine scan cannot see: everything the registry group `data-config-trees`
names (until HOS-240, 2026-10-13, the `build/config` record kind) and the
directory holding the registry, except the registry itself and this mode's
own ledger. Files a reader parses (`.json`, `.yaml`, `.yml`) are read as
structure, so only VALUES are scanned: a comment is gone after parsing, and the
value of a prose key (`_comment`, `_schema`, `what`) explains rather than
addresses and is skipped. Every other file is a document or a word list, not
config; it is not scanned, and the report names each one rather than counting
it as clean. A file that does not parse is refused (exit 2), never read as
empty. A path in a config is engine-relative on purpose in one reader
(`include` of the service manifest) and VM-relative in another; the scan
cannot tell those from a data path, which is what the ledger is for.

Its ledger is the registry name `data-config-exemptions` in the data
repository, same format, and it is OPTIONAL: absent, the ceiling is 0 and no
entry exempts anything, which is the goal this mode exists to hold.

Exit 0 clean, 1 a rise, a fall, an uncovered or stale entry, 2 unusable input.
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import re
import subprocess
import sys
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE_ROOT))

from scripts.heading_os_2.families import (  # noqa: E402
    NOT_A_RESOURCE_NAME,
    is_valid,
    subject_roots,
)
from scripts.utils.statement_exemptions import (  # noqa: E402
    ExemptionsError,
    entries_from,
    line_key,
    match,
)

#: Relative to the engine root being scanned.
RATCHET_REL = Path("config/skill-data-paths-exemptions.json")
SKILLS_REL = Path(".claude/skills")
ENGINE_RATCHET_REL = Path("config/engine-data-paths-exemptions.json")
#: The engine-code roots `--engine` scans.
ENGINE_SCAN_ROOTS = ("scripts", ".claude/hooks", "config")
#: A line carrying this is a site `scripts/leak-guard.py` already reads as reviewed.
REVIEWED_MARKER = "leak-guard: ok"
#: Not scanned by `--engine`: the address book itself, whose job is to hold the
#: physical addresses every other file asks for by name, and
#: the gates' exemption ledgers, which quote their findings verbatim.
REGISTRY_FILES = frozenset({"scripts/data-names.example.yaml",
                            "scripts/runtime-services.example.yaml"})
EXEMPTION_LEDGERS = "config/*-exemptions.json"

FAMILY_ROOTS = ("canon", "ledger", "status", "research", "work", "inbound",
                "build", "trace", "asset")
# The C3 subject-tree roots joined 2026-10-13, from `families.subject_roots()`.
LEGACY_DIRS = ("outputs", "datastore", "crm", "threads", "knowledge", "plans",
               "auto-memory", "context")
_ROOTS = "|".join(FAMILY_ROOTS + subject_roots() + LEGACY_DIRS)
_TAIL = r"[\w.{}<>*-]+(?:/[\w.{}<>*-]+)*"
PATTERN = re.compile(
    r"(?<![\w:/.$-])((?:%s)/%s%s)" % (_ROOTS, NOT_A_RESOURCE_NAME, _TAIL))
#: `--engine` only: the same address behind a placeholder that stands for the
#: data root (`<data>`, `<data-root>`, `{data_root}` and their spellings), which
#: PATTERN's look-behind passes because a `/` precedes the root.
DATA_ROOT_PLACEHOLDER = re.compile(
    r"(?:<data(?:[-_]root)?>|\{data(?:_root)?\})/((?:%s)/%s%s)"
    % (_ROOTS, NOT_A_RESOURCE_NAME, _TAIL))
#: `--engine` only, Python: a run of string segments joined into a path
#: (`root / "crm" / "contacts"`, `Path("crm", "contacts")`, `os.path.join`)
#: holds no slash-joined literal, so the joined run is matched instead.
JOIN_CALLS = frozenset({"join", "joinpath", "Path", "PurePath", "PurePosixPath"})
#: `--engine` only, Python: the resolvers whose constant first argument names
#: the directory a join starts from.
NAME_RESOLVERS = frozenset({"data_path", "data_read", "data_write_dir"})
#: The registry the engine ships, which `named_subdir_joins` reads children from.
DEFAULT_REGISTRY_REL = Path("scripts/data-names.example.yaml")

#: `--data-config`: the keys whose value is prose, in any structured config.
PROSE_KEYS = frozenset({"_comment", "_schema", "what"})
#: `--data-config`: what a config reader parses; anything else is a document.
STRUCTURED_SUFFIXES = frozenset({".json", ".yaml", ".yml"})

#: The text files a skill carries. Scanned whole, frontmatter included.
SUFFIXES = {".md", ".py", ".json", ".yaml", ".yml", ".txt", ".html"}


def skill_files(root: Path) -> list[Path]:
    """Every text file under a directory of `.claude/skills/` that holds a SKILL.md."""
    out: list[Path] = []
    base = root / SKILLS_REL
    if not base.is_dir():
        return out
    for skill in sorted(base.iterdir()):
        if not (skill / "SKILL.md").is_file():
            continue
        for f in sorted(skill.rglob("*")):
            if f.is_file() and f.suffix in SUFFIXES and "__pycache__" not in f.parts:
                out.append(f)
    return out


def engine_files(root: Path) -> list[Path]:
    """Every text file git tracks or would carry under ENGINE_SCAN_ROOTS."""
    proc = subprocess.run(  # nosec B603 B607 - fixed argv, no shell
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
         "--exclude-standard", "--", *ENGINE_SCAN_ROOTS],
        capture_output=True, check=True)
    out: list[Path] = []
    for rel in sorted(set(proc.stdout.decode("utf-8").split("\0"))):
        if not rel or rel in REGISTRY_FILES or fnmatch.fnmatch(rel, EXEMPTION_LEDGERS):
            continue
        f = root / rel
        if not f.is_file() or b"\0" in f.read_bytes()[:8192]:
            continue
        out.append(f)
    return out


def is_kind_name(literal: str) -> bool:
    """True for a bare `<family>/<leaf>` the record vocabulary defines.

    That pair is the name of a KIND, the same one `record:<family>/<leaf>`
    asks for, and code and comments use it to say which kind they mean. A
    third segment makes it an address under the kind, and that is a finding.
    """
    parts = literal.split("/")
    return len(parts) == 2 and is_valid(parts[0], parts[1])


def _segments(node: ast.AST) -> list[ast.AST] | None:
    """The operands a path expression joins, or None when it joins nothing."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        out: list[ast.AST] = []
        for side in (node.left, node.right):
            out.extend(_segments(side) or [side])
        return out
    if isinstance(node, ast.Call) and not node.keywords:
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
        if name in JOIN_CALLS:
            # `base.joinpath(...)` joins onto its receiver, which may hold a segment too.
            head: list[ast.AST] = []
            if name == "joinpath" and isinstance(f, ast.Attribute):
                head = _segments(f.value) or [f.value]
            return head + list(node.args)
    return None


def joined_literals(text: str) -> list[tuple[int, str]]:
    """(line, `a/b/...`) for every run of string segments a path joins.

    Only the outermost join is read, and only a run of two or more constant
    segments whose join PATTERN matches from its first character; a single
    constant is the lexical scan's.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError) as exc:
        print(f"warning: not parsed for joined segments ({exc})", file=sys.stderr)
        return []
    inner: set[int] = set()
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if id(node) in inner:
            continue
        segs = _segments(node)
        if segs is None:
            continue
        for sub in ast.walk(node):
            if sub is not node:
                inner.add(id(sub))
        run: list[str] = []
        for seg in segs + [None]:
            if isinstance(seg, ast.Constant) and isinstance(seg.value, str):
                run.append(seg.value.strip("/"))
                continue
            if len(run) >= 2:
                joined = "/".join(run)
                m = PATTERN.match(joined)
                if m and not any(PATTERN.search(r) for r in run):
                    out.append((node.lineno, m.group(1)))
            run = []
    return out


def registry_children(root: Path) -> dict[str, dict[str, str]]:
    """{parent name: {segment: child name}} for every registry name whose address
    is one segment below another name's, in the engine's default registry."""
    import yaml

    names = yaml.safe_load((root / DEFAULT_REGISTRY_REL).read_text(encoding="utf-8"))
    by_address: dict[tuple[str, str, str], str] = {}
    for name, entry in ((names or {}).get("names") or {}).items():
        if not isinstance(entry, dict):
            continue
        if "path" in entry:
            by_address[("path", "", entry["path"])] = name
    children: dict[str, dict[str, str]] = {}
    for (kind, base, rel), name in by_address.items():
        if not rel or "{" in rel:
            continue
        parent_rel, _, segment = rel.rpartition("/")
        parent = by_address.get((kind, base, parent_rel))
        if parent:
            children.setdefault(parent, {})[segment] = name
    return children


def named_subdir_joins(text: str, children: dict[str, dict[str, str]]) -> list[tuple[int, str]]:
    """(line, `<parent> / "<segment>" -> <child>`) for every join of a named
    child's segment onto a root that resolves to its parent name."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError) as exc:
        print(f"warning: not parsed for named-root joins ({exc})", file=sys.stderr)
        return []

    def resolver_name(node: ast.AST, local: dict[str, str]) -> str | None:
        if not isinstance(node, ast.Call):
            return None
        fn = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
        if (fn in NAME_RESOLVERS and node.args and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            return node.args[0].value
        return local.get(fn) if fn and not node.args else None

    functions: dict[str, str] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and not fn.args.args:
            returns = [r for r in ast.walk(fn) if isinstance(r, ast.Return)]
            if len(returns) == 1 and returns[0].value is not None:
                name = resolver_name(returns[0].value, {})
                if name:
                    functions[fn.name] = name

    out: list[tuple[int, str]] = []
    scopes = [tree] + [n for n in ast.walk(tree)
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    seen: set[int] = set()
    for scope in scopes:
        assigned: dict[str, list[str | None]] = {}
        for node in ast.walk(scope):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        assigned.setdefault(target.id, []).append(
                            resolver_name(node.value, functions))
        variables = {v: names[0] for v, names in assigned.items()
                     if len(names) == 1 and names[0]}
        for node in ast.walk(scope):
            if (id(node) in seen or not isinstance(node, ast.BinOp)
                    or not isinstance(node.op, ast.Div)
                    or not isinstance(node.right, ast.Constant)
                    or not isinstance(node.right.value, str)):
                continue
            left = node.left
            parent = (variables.get(left.id) if isinstance(left, ast.Name)
                      else resolver_name(left, functions))
            segment = node.right.value.strip("/").split("/")[0]
            child = children.get(parent or "", {}).get(segment)
            if child:
                seen.add(id(node))
                out.append((node.lineno, f'{parent} / "{segment}" -> {child}'))
    return out


def scan_file(path: Path, root: Path, engine: bool = False,
              children: dict[str, dict[str, str]] | None = None) -> list[dict]:
    """Every finding in one file, with the region it sits in."""
    rel = path.relative_to(root).as_posix()
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    fm_end = 0
    if path.name == "SKILL.md" and lines and lines[0] == "---":
        for i, ln in enumerate(lines[1:], 2):
            if ln == "---":
                fm_end = i
                break
    found = []

    def add(n: int, literal: str) -> None:
        found.append({
            "path": rel,
            "line": n,
            "region": "frontmatter" if n <= fm_end else "body",
            "literal": literal,
            "text": lines[n - 1],
        })

    for n, line in enumerate(lines, 1):
        if engine and REVIEWED_MARKER in line:
            continue
        for m in PATTERN.finditer(line):
            if engine and is_kind_name(m.group(1)):
                continue
            add(n, m.group(1))
        if engine:
            for m in DATA_ROOT_PLACEHOLDER.finditer(line):
                add(n, "<data>/" + m.group(1))
    if engine and path.suffix == ".py":
        for n, literal in joined_literals(text):
            if REVIEWED_MARKER not in lines[n - 1]:
                add(n, literal)
        for n, literal in named_subdir_joins(text, children or {}):
            if REVIEWED_MARKER not in lines[n - 1]:
                add(n, literal)
    found.sort(key=lambda f: f["line"])
    return found


def data_config_files(data_root: Path) -> tuple[list[Path], list[Path], Path]:
    """(files to scan, files not scanned, ledger) under the data repository's config trees."""
    from scripts.utils.data_path import data_path, registry_path

    registry = registry_path(data_root)
    ledger = data_path("data-config-exemptions", data_root=data_root)
    scan: list[Path] = []
    skipped: list[Path] = []
    seen: set[Path] = set()
    # HOS-240, 2026-10-13: the config trees are the registry group
    # `data-config-trees`, no longer the `build/config` record kind's root.
    from scripts.utils.data_homes import homes

    bases = [*homes("data-config-trees", data_root=data_root), registry.parent]
    for base in bases:
        members = [base] if base.is_file() else sorted(base.rglob("*")) if base.is_dir() else []
        for f in members:
            if (not f.is_file() or f in seen or f in (registry, ledger)
                    or "__pycache__" in f.parts):
                continue
            seen.add(f)
            (scan if f.suffix in STRUCTURED_SUFFIXES else skipped).append(f)
    return scan, skipped, ledger


def _config_values(node, out: list) -> None:
    """Every string scalar under a composed YAML/JSON node, prose keys left out."""
    import yaml

    if isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            if not (isinstance(key, yaml.ScalarNode) and key.value in PROSE_KEYS):
                _config_values(value, out)
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            _config_values(item, out)
    elif isinstance(node, yaml.ScalarNode) and node.tag.endswith(":str"):
        out.append((node.start_mark.line + 1, node.value))


def scan_config_file(path: Path, data_root: Path) -> list[dict]:
    """Every physical data path among one structured config file's values.

    Raises ValueError when the file does not parse: an unread config is not a
    clean one.
    """
    import yaml

    text = path.read_text(encoding="utf-8")
    try:
        tree = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: does not parse ({exc})") from exc
    values: list[tuple[int, str]] = []
    if tree is not None:
        _config_values(tree, values)
    lines = text.splitlines()
    rel = path.relative_to(data_root).as_posix()
    found = []
    for n, value in values:
        for m in PATTERN.finditer(value):
            if not is_kind_name(m.group(1)):
                found.append({"path": rel, "line": n, "region": "value",
                              "literal": m.group(1), "text": lines[n - 1]})
    return found


def load_ratchet(root: Path, rel: Path = RATCHET_REL) -> tuple[int, list]:
    path = root / rel
    data = json.loads(path.read_text(encoding="utf-8"))
    ceiling = data.get("ceiling") if isinstance(data, dict) else None
    if not isinstance(ceiling, int) or ceiling < 0:
        raise ExemptionsError(f"{path}: `ceiling` must be a non-negative integer")
    return ceiling, entries_from(data, str(path))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="exit 1 unless the tree matches the ratchet")
    ap.add_argument("--json", action="store_true", help="machine-readable report")
    ap.add_argument("--root", type=Path, default=ENGINE_ROOT,
                    help="engine checkout to scan (default: the one this file is in)")
    ap.add_argument("--engine", action="store_true",
                    help="scan engine code (scripts, hooks, config) instead of skills")
    ap.add_argument("--data-config", action="store_true",
                    help="scan the data repository's config files instead of skills")
    ap.add_argument("--data-root", type=Path, default=None,
                    help="with --data-config: the data repository (default: the resolved data root)")
    args = ap.parse_args()
    if args.engine and args.data_config:
        ap.error("--engine and --data-config are two scans; pick one")
    root = args.root.resolve()
    ratchet_rel = ENGINE_RATCHET_REL if args.engine else RATCHET_REL
    skipped: list[Path] = []

    if args.data_config:
        from scripts.utils.data_path import DataPathError
        from scripts.utils.paths import DataRootError, get_data_root
        try:
            root = (args.data_root or get_data_root()).resolve()
            files, skipped, ledger = data_config_files(root)
        except (DataPathError, DataRootError, OSError) as exc:
            print(f"cannot list the data repository's config files: {exc}", file=sys.stderr)
            return 2
        ratchet_rel = ledger.relative_to(root)
        if not files:
            print(f"REFUSED: no structured config file found under {root}; "
                  f"refusing to report a clean scan of nothing", file=sys.stderr)
            return 2
    try:
        if args.data_config and not (root / ratchet_rel).exists():
            ceiling, entries = 0, []
            print(f"no ledger at {ratchet_rel}: ceiling 0, no exemptions", file=sys.stderr)
        else:
            ceiling, entries = load_ratchet(root, ratchet_rel)
    except (OSError, json.JSONDecodeError, ExemptionsError) as exc:
        print(f"unusable ratchet file: {exc}", file=sys.stderr)
        return 2

    if args.data_config:
        try:
            findings = [f for p in files for f in scan_config_file(p, root)]
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2
    elif args.engine:
        try:
            files = engine_files(root)
        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"cannot list engine files: {exc}", file=sys.stderr)
            return 2
    else:
        files = skill_files(root)
    if not args.data_config:
        children = registry_children(root) if args.engine else None
        findings = [f for p in files for f in scan_file(p, root, args.engine, children)]
    uncovered_keys, stale = match(((f["path"], line_key(f["text"])) for f in findings), entries)
    # `match` returns keys, not findings; take that many findings per key, in file order.
    want: dict[tuple[str, str], int] = {}
    for k in uncovered_keys:
        want[k] = want.get(k, 0) + 1
    uncovered = []
    for f in findings:
        k = (f["path"], line_key(f["text"]))
        if want.get(k, 0) > 0:
            want[k] -= 1
            uncovered.append(f)
    count = len(uncovered)
    if args.engine or args.data_config:
        units, unit = sorted({f["path"] for f in uncovered}), "file"
    else:
        units, unit = sorted({f["path"].split("/")[2] for f in uncovered}), "skill"

    if args.json:
        print(json.dumps({"ceiling": ceiling, "uncovered": count, f"{unit}s": len(units),
                          "exempt": len(findings) - count,
                          "stale_entries": [e._asdict() for e in stale],
                          "not_scanned": [p.relative_to(root).as_posix() for p in skipped],
                          "findings": uncovered}, ensure_ascii=False, indent=1))
    else:
        for f in uncovered:
            print(f"{f['path']}:{f['line']} [{f['region']}] {f['literal']}")
        for p in skipped:
            print(f"not scanned (not a structured config): {p.relative_to(root).as_posix()}")
        for e in stale:
            print(f"STALE entry, matches no finding: {e.path}: {e.statement!r}")
        print(f"{count} physical data path finding(s) in {len(units)} {unit}(s), "
              f"{len(findings) - count} exempt by reviewed entry; ceiling {ceiling}")

    if not args.check:
        return 0
    bad = False
    if count > ceiling:
        print(f"REFUSED: {count} findings exceed the ceiling of {ceiling}. Resolve the "
              f"address through `python scripts/data-path.py path <name>`, or record a "
              f"reviewed entry in {ratchet_rel}.", file=sys.stderr)
        bad = True
    elif count < ceiling:
        print(f"REFUSED: {count} findings, below the ceiling of {ceiling}. Lower "
              f"`ceiling` in {ratchet_rel} to {count}; it may only fall.", file=sys.stderr)
        bad = True
    if stale:
        print(f"REFUSED: {len(stale)} stale exemption entr(y/ies) in {ratchet_rel}.",
              file=sys.stderr)
        bad = True
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
