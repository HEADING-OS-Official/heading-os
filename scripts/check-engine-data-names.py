#!/usr/bin/env python3
"""Every data-address name the engine asks for, held against what a FRESH data repository answers.

    python scripts/check-engine-data-names.py            # report
    python scripts/check-engine-data-names.py --check    # the gate: exit 1 on a name a fresh repository cannot answer
    python scripts/check-engine-data-names.py --json

WHY (HOS-298). A data repository created by `scripts/create-data-repo.py` is
born with a registry that carries the `services:` rows of
`scripts/runtime-services.example.yaml` and NO `names:` or `groups:` block, so
every bare name and group is answered from `scripts/data-names.example.yaml`
(`data_path.load_registry`, case 3). Those two files are edited by hand and the
engine's call sites grow by hand, so the defaults trailed the code silently.
MEASURED 2026-11-01 on a tree scaffolded by `scripts/init-data.py` from this
checkout: `runtime:settings-backup/state`, `threads-personal`,
`odin-positions`, `odin-sources` and `odin-episodes` all refused, so
`setup-platform.sh` took no settings backup, `thread.py list` failed and
`memory-index.py build` skipped the Odin layer on every new executive's
install, and nothing said so until a tester read the errors.

WHAT IS COLLECTED, and from where:

  * Engine Python, through the AST (never a text match): the first argument of
    every call to `data_path`, `data_write_dir`, `data_write_file`,
    `data_read`, `data_read_required` and `group_names` when it is a string
    literal. Files under `tests/` are left out: their names are invented for
    synthetic trees.
  * `data-path.py <path|read|each|group> [--flag ...] <name>`, in every other
    tracked text file (shell, skills, rules, config), in every Python string
    literal that is not a bare string statement (a docstring describes a
    command rather than running it), and in every Python list or tuple literal
    that spells an argv for it (`[python, str(DATA_PATH), "path", "x"]`).
  * `config/memory-index.yaml`: each layer's `names:` and `files:` (registry
    names) and `groups:` and `file_groups:` (group names).
  * The members of every group the engine's default ships.

WHAT IS NOT COLLECTED, stated rather than implied. A name built at run time
(an f-string, a variable, a name handed through a wrapper of the engine's own)
cannot be read from the source; the count of such calls is printed so the
reader sees how much the method does not reach. A name spelled in prose outside
a `data-path.py` command is not read.

WHAT "A FRESH REPOSITORY ANSWERS" MEANS. Exactly what `load_registry`,
`load_groups` and `load_services` fall back to on a tree with no block of its
own: `template_names()`, `template_groups()` and `template_services()` with
the `KINDS` of `runtime_dir`, read from the inspected tree's own two template
files through the same parsers. It creates no directory and writes nothing.

INSTALLER-PLACED names. A name whose address the public default may not carry
is listed by name only under `installer_placed:` in
`scripts/data-names.example.yaml`, and `scripts/init-data.py` writes its
address into the new tree's registry (`INSTALLER_ADDRESSES`, read here from
the source through the AST, never imported). Such a name counts as answered
only when it is in BOTH and `data_path.installer_address_problems`, the rule
init-data.py itself applies before it scaffolds, finds nothing wrong with its
address; otherwise it refuses.

EXEMPT names. `tests/test_engine_tree_clean.py` refuses an address in the
shipped default that is one operator's own artifact (a dated directory, or a
file that is not a configuration or context record), because that file goes to
a public repository. A name the engine asks for whose only honest address has
that shape is listed in `EXEMPT` below with its reason, and on a fresh
repository it refuses at the call, which is the truthful outcome. An `EXEMPT`
entry that no collected name needs any more, or that the defaults now answer,
refuses as stale.

Exit 0 when every collected name is answered or exempt, 1 when one is not, 2
when the collection itself collapsed (no Python file read, or fewer names than
the floor), because a guard that inspected nothing has established nothing.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.colors import BOLD, GRAY, GREEN, RED, RESET  # noqa: E402
from scripts.utils import data_path as _dp  # noqa: E402
from scripts.utils import runtime_dir as _rt  # noqa: E402
from scripts.utils.data_path import RUNTIME_SCHEME  # noqa: E402
from scripts.utils.registry_bootstrap import subject_roots_from_files  # noqa: E402
from scripts.utils.runtime_dir import KINDS  # noqa: E402
from scripts.utils.workspace import get_workspace_root  # noqa: E402

#: The resolver's entry points whose first argument is a registry name.
NAME_CALLS = frozenset({"data_path", "data_write_dir", "data_write_file",
                        "data_read", "data_read_required"})
#: ... and the one whose first argument is a group name.
GROUP_CALLS = frozenset({"group_names"})

#: `data-path.py <verb> [--flag ...] <name>` in a shell line, a skill or a rule.
CLI_RE = re.compile(
    r"""data-path\.py["']?\s+(path|read|each|group)\s+(?:--[a-z-]+\s+)*["']?"""
    r"""([a-z][a-z0-9_-]*(?::[a-z0-9_-]+/[a-z0-9_-]+)?)""")

CLI_VERBS = frozenset({"path", "read", "each", "group"})
NAME_RE = re.compile(r"[a-z][a-z0-9_-]*(?::[a-z0-9_-]+/[a-z0-9_-]+)?")

MEMORY_INDEX_CONFIG = "config/memory-index.yaml"
INSTALLER = "scripts/init-data.py"

#: Below this, the corpus collapsed rather than shrank. MEASURED 2026-11-01:
#: 271 distinct names and groups over this tree.
FLOOR = 200

#: Names the engine asks for whose only honest address is one operator's own
#: artifact, which the shipped default may not carry (see the docstring).
EXEMPT: dict[str, str] = {
    "email-signature-html": "an operator's own HTML signature document; the public default may name no .html file",
    "email-signature-logo": "an operator's own signature logo image; the public default may name no image file",
    "email-signature-divider": "an operator's own signature divider image; the public default may name no image file",
    "docs-theme-css": "an operator's own stylesheet for the portable guides; the public default may name no .css file",
    "heading-os-logo": "the HEADING OS site's own logo image inside one dated site work directory, not structure",
    "heading-os-favicon": "the HEADING OS site's own favicon inside one dated site work directory, not structure",
    "tribe-workbook": "one operator's own team roster spreadsheet; the public default may name no office document",
    "cognee-pilot-outputs": "the outputs of one dated pilot run on one operator's tree, never structure",
    "ho2-migration-workdir": "the archived working directory of one dated migration of one operator's tree, never structure",
    "xpager-reference": "one dated xPager deliverable kept for visual continuity, never structure",
}

def tracked(root: Path) -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True,
                         capture_output=True).stdout.decode("utf-8", "replace")
    return [p for p in out.split("\0") if p]


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _argv_commands(elts: list[ast.expr]) -> list[tuple[str, str]]:
    """`(verb, name)` for each `data-path.py <verb> [--flag ...] <name>` in an argv literal.

    The script element may be any expression that spells `data-path.py`
    (`str(ROOT / "scripts" / "data-path.py")`); the verb and the name must be
    string literals, and a name that is not one is not readable from source.
    """
    out = []
    for i, elt in enumerate(elts):
        if "data-path.py" not in ast.unparse(elt):
            continue
        rest = [e.value if isinstance(e, ast.Constant) and isinstance(e.value, str) else None
                for e in elts[i + 1:]]
        if not rest or rest[0] not in CLI_VERBS:
            continue
        args = [a for a in rest[1:] if a is None or not a.startswith("--")]
        if args and args[0] is not None and NAME_RE.fullmatch(args[0]):
            out.append((rest[0], args[0]))
    return out


def collect(root: Path) -> tuple[dict[tuple[str, str], set[str]], dict[str, int]]:
    """`{(kind, name): {where, ...}}`, kind `name` or `group`, plus the counters."""
    found: dict[tuple[str, str], set[str]] = {}
    stats = {"python_files": 0, "text_files": 0, "dynamic_calls": 0,
             "undecodable_files": 0}

    def add(kind: str, name: str, where: str) -> None:
        found.setdefault((kind, name), set()).add(where)

    for rel in tracked(root):
        path = root / rel
        if rel.startswith("tests/") or not path.is_file():
            continue
        if rel.endswith(".py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            except (OSError, UnicodeDecodeError, SyntaxError) as exc:
                print(f"{RED}cannot parse {rel}: {exc}{RESET}", file=sys.stderr)
                raise
            stats["python_files"] += 1
            # Bare string statements (docstrings) describe commands rather
            # than run them, so they are left to the reader.
            prose = {id(n.value) for n in ast.walk(tree)
                     if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and id(node) not in prose):
                    for m in CLI_RE.finditer(node.value):
                        add("group" if m.group(1) == "group" else "name", m.group(2),
                            f"{rel}:{node.lineno}")
                    continue
                if isinstance(node, (ast.List, ast.Tuple)):
                    for verb, name in _argv_commands(node.elts):
                        add("group" if verb == "group" else "name", name,
                            f"{rel}:{node.lineno}")
                    continue
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                fn = _call_name(node.func)
                if fn not in NAME_CALLS and fn not in GROUP_CALLS:
                    continue
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    add("group" if fn in GROUP_CALLS else "name", arg.value,
                        f"{rel}:{node.lineno}")
                else:
                    stats["dynamic_calls"] += 1
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # Counted and reported: a binary file names no address, and a text
            # file with one bad byte would be skipped too, so the reader sees it.
            stats["undecodable_files"] += 1
            continue
        stats["text_files"] += 1
        for lineno, line in enumerate(text.splitlines(), 1):
            for m in CLI_RE.finditer(line):
                add("group" if m.group(1) == "group" else "name", m.group(2),
                    f"{rel}:{lineno}")

    config = yaml.safe_load((root / MEMORY_INDEX_CONFIG).read_text(encoding="utf-8"))
    for layer in config.get("layers") or []:
        for key, kind in (("names", "name"), ("files", "name"),
                          ("groups", "group"), ("file_groups", "group")):
            for name in layer.get(key) or []:
                add(kind, name, f"{MEMORY_INDEX_CONFIG} (layer {layer.get('layer')})")
    return found, stats


def unanswered(kind: str, name: str, names: dict, groups: dict,
               services: dict) -> str | None:
    """Why a fresh repository cannot answer this name, or None when it can."""
    if kind == "group":
        return None if name in groups else "no group of that name in the shipped default"
    if name.startswith(RUNTIME_SCHEME):
        service, _, kind_part = name[len(RUNTIME_SCHEME):].partition("/")
        if service not in services:
            return f"runtime service {service!r} has no row in the shipped services template"
        if kind_part not in KINDS:
            return f"runtime kind {kind_part!r} is not one of {', '.join(KINDS)}"
        return None
    return None if name in names else "no row in the shipped default names"


def shipped_defaults(root: Path) -> tuple[dict, dict, dict, set[str]]:
    """The names, groups, services and installer-placed names a fresh repository is answered from.

    Read from the two template files of the tree under inspection, through the
    resolver's own parsers, so on the running checkout this is exactly
    `template_names()`, `template_groups()` and `template_services()`, and a
    test can hold an older tree's templates to the same question.
    """
    names_path = root.joinpath(*_dp.EXAMPLE_NAMES_REL)
    names_text = names_path.read_text(encoding="utf-8")
    names = _dp._parse_names_document(names_text, names_path)
    groups = dict(_dp._parse_document_disk(names_text, names_path, None)[_dp.GROUPS_KEY] or {})
    services_path = root.joinpath(*_rt.EXAMPLE_SERVICES_REL)
    raw = yaml.safe_load(services_path.read_text(encoding="utf-8")) or {}
    services = _rt._validate_services(raw.get(_dp.SERVICES_KEY) or {}, services_path)
    listed = set(_dp._parse_document_disk(names_text, names_path, None)
                 [_dp.INSTALLER_PLACED_KEY] or ())
    return names, groups, services, listed


def installer_addresses(root: Path) -> dict:
    """`INSTALLER_ADDRESSES` of this tree's `scripts/init-data.py`, from its source.

    Read through the AST with `ast.literal_eval`, so nothing of that tree is
    executed and an older tree with no such constant answers with none.
    """
    tree = ast.parse((root / INSTALLER).read_text(encoding="utf-8"), filename=INSTALLER)
    for node in tree.body:
        target = (node.target if isinstance(node, ast.AnnAssign)
                  else node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1
                  else None)
        if isinstance(target, ast.Name) and target.id == "INSTALLER_ADDRESSES":
            try:
                value = ast.literal_eval(node.value)
            except ValueError as exc:
                raise SystemExit(f"{INSTALLER}: INSTALLER_ADDRESSES is not a literal ({exc})") from exc
            if not isinstance(value, dict):
                raise SystemExit(f"{INSTALLER}: INSTALLER_ADDRESSES is not a dict literal")
            return value
    return {}


def judge(root: Path) -> dict:
    found, stats = collect(root)
    names, groups, services, listed = shipped_defaults(root)
    addresses = installer_addresses(root)
    # The installer's own rule, shared rather than copied: a name whose address
    # init-data.py would refuse is not answered on any fresh repository.
    problems = _dp.installer_address_problems(addresses, subject_roots_from_files(None, root))
    refused = {p.split(":", 1)[0] for p in problems}
    addressed = set(addresses)
    placed = (listed & addressed) - refused
    names = {**names, **{n: {} for n in placed}}
    mismatch = [(n, "listed under `installer_placed:` and placed by no "
                    f"INSTALLER_ADDRESSES row of {INSTALLER}") for n in sorted(listed - addressed)]
    mismatch += [(n, f"placed by {INSTALLER} and not listed under `installer_placed:`")
                 for n in sorted(addressed - listed)]
    for group, members in groups.items():
        for member in members:
            found.setdefault(("name", member), set()).add(f"group {group} (shipped default)")
    missing, exempt_used = [], set()
    for (kind, name), where in sorted(found.items()):
        why = unanswered(kind, name, names, groups, services)
        if why is None:
            continue
        if kind == "name" and name in EXEMPT:
            exempt_used.add(name)
            continue
        missing.append({"kind": kind, "name": name, "why": why, "where": sorted(where)})
    mismatch += [(p.split(":", 1)[0], f"{INSTALLER} would refuse its address: "
                                      f"{p.split(':', 1)[1].strip()}") for p in problems]
    for name, why in mismatch:
        missing.append({"kind": "name", "name": name, "why": why, "where": [INSTALLER]})
    stale = []
    for name in sorted(EXEMPT):
        if ("name", name) not in found:
            stale.append(f"{name}: no engine call asks for it any more")
        elif name not in exempt_used:
            stale.append(f"{name}: the shipped default now answers it")
    return {"stats": stats, "collected": len(found), "missing": missing,
            "stale_exemptions": stale, "exempt": sorted(exempt_used),
            "installer_placed": sorted(placed)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="exit 1 on any unanswered name")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--root", type=Path, default=None, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    root = (args.root or get_workspace_root()).resolve()
    result = judge(root)
    stats = result["stats"]
    collapsed = stats["python_files"] == 0 or result["collected"] < FLOOR
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"{BOLD}engine data names vs a fresh data repository{RESET}: "
              f"{result['collected']} names and groups collected from "
              f"{stats['python_files']} Python and {stats['text_files']} text files "
              f"({stats['undecodable_files']} undecodable files not read); "
              f"{stats['dynamic_calls']} calls build their name at run time and are "
              f"not checked; {len(result['exempt'])} exempt; placed by the "
              f"installer: {', '.join(result['installer_placed']) or 'none'}.")
        for m in result["missing"]:
            print(f"  {RED}UNANSWERED{RESET} {m['kind']} {m['name']}: {m['why']}")
            for w in m["where"][:5]:
                print(f"      {GRAY}{w}{RESET}")
        for s in result["stale_exemptions"]:
            print(f"  {RED}STALE EXEMPTION{RESET} {s}")
        if collapsed:
            print(f"{RED}the collection collapsed (floor {FLOOR}); nothing is established{RESET}")
        elif not result["missing"] and not result["stale_exemptions"]:
            print(f"{GREEN}every collected name is answered by the shipped defaults "
                  f"or exempt{RESET}")
    if collapsed:
        return 2
    if args.check and (result["missing"] or result["stale_exemptions"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
