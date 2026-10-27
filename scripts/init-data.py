#!/usr/bin/env python3
"""Scaffold a private data folder for the HEADING OS engine, in the C3 subject-tree layout.

Creates the empty data tree the engine expects, stamps the schema version, and
seeds the data-address registry. Refuses to clobber an existing non-empty data
folder.

WHAT THE TREE IS, and where each part comes from. Nothing here is a list of
directories typed by hand:

  * One directory per SUBJECT ROOT, read from the `subject_roots:` line of the
    engine's default registry (`scripts/data-names.example.yaml`) through
    `registry_bootstrap.subject_roots_from_files`, the same reader every guard
    asking "is this a data root" uses. A root added there is scaffolded the
    same day. The fresh target carries no registry of its own yet, so the
    engine's default is the one that answers.
  * The `services:` block of the registry, copied from the engine's template.
  * The `.schema-version` stamp.

History, kept short. Until 2026-09-25 this file typed the pre-move layout by
hand. From then until 2026-10-13 it created one directory per HEADING OS 2
kind, `<family>/<leaf>`, and wrote the family manifest under `_manifest/`, so
the `record:` base of the registry could resolve on a fresh tree. HOS-240
retired that base: every name the engine ships is now a `path:` into the
subject tree (or a `runtime:` address), so there is no manifest to write and no
family root to create.

The registry is seeded here rather than in `create-data-repo.py` because this
script is the single source of truth for the layout (that script's own
docstring says so and delegates here), and because a data root created by this
script alone must still be usable. Since 2026-09-19 the registry carries the
runtime SERVICE vocabulary as well as the residue addresses (HOS-5 § 7.2), so a
data root without one names no service and refuses every runtime writer.

Only the `services:` block is COPIED, and the `names:` block is deliberately
not: the engine answers for it out of `scripts/data-names.example.yaml` until
this root grows a block of its own, which `data_path.load_registry` states as
its case 3. Copying it would make a snapshot of the engine's defaults into this
root's own file on the day it was created, after which the root would stop
tracking them and the operator would be maintaining lines nobody asked him for.
Every `path:` base in that file must start at a top-level directory this
scaffold can hold; `init` refuses before writing anything if one does not,
because a name the fresh tree cannot answer is a defect in the engine's own
defaults.

Usage:
  python scripts/init-data.py [--path DIR]   # default: ../.heading-os-data
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.paths import (DATA_SCHEMA_VERSION, engine_siblings_root,
                                 get_workspace_root)
from scripts.utils.atomic import atomic_write_text
from scripts.utils.data_path import example_names_path, template_names
from scripts.utils.registry_bootstrap import subject_roots_from_files
from scripts.utils.runtime_dir import example_services_path, seed_services

#: Top-level directories outside the subject tree that the engine's default
#: names legitimately address: the automatic memory records and the archives of
#: them taken before a move. Anything else outside the subject roots refuses.
MEMORY_TOP_LEVEL = ("auto-memory", "memory-archive")


def scaffold_roots() -> tuple[str, ...]:
    """Every subject root the fresh tree gets, after checking the shipped names fit.

    The engine's default `names:` file is held against the roots, so a shipped
    `path:` base whose first segment the fresh tree cannot hold refuses here,
    before anything is written, rather than on the first lookup.
    """
    roots = subject_roots_from_files(None, get_workspace_root())
    allowed = set(roots) | set(MEMORY_TOP_LEVEL)
    stray = sorted({f"{name} ({e['path']})"
                    for name, e in template_names().items()
                    # A single-segment path is a FILE at the data root, which
                    # every tree can hold; only a directory base must be a root.
                    if e["base"] == "path" and "/" in e["path"]
                    and e["path"].split("/")[0] not in allowed})
    if stray:
        raise SystemExit(
            f"Refusing to scaffold: {example_names_path()} names addresses outside "
            f"the subject roots {', '.join(roots)}: {', '.join(stray)}.")
    return roots


def init_data(target: Path) -> int:
    # Not-a-directory is checked before emptiness, because `iterdir()` is what
    # raises. Measured 2026-08-29 with `touch /tmp/fake-data-probe` and
    # `--path /tmp/fake-data-probe`: `any(target.iterdir())` raised
    # NotADirectoryError before `any()` could reach a decision, so the operator
    # got a traceback where the next line promises a refusal. A symlink counts
    # here too, both because a broken one answers False to `exists()` and would
    # fall through to the mkdir, and because this workspace does not use them.
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        print(f"Refusing to scaffold: {target} exists and is not a directory.")
        return 1
    if target.is_dir() and any(target.iterdir()):
        print(f"Refusing to scaffold: {target} exists and is not empty.")
        return 1
    roots = scaffold_roots()
    for root in roots:
        (target / root).mkdir(parents=True, exist_ok=True)
    # A writer of this marker. migrate-data.py goes through atomic_write_text
    # too (build_data_repo.py did, until its retirement on 2026-09-28); a half-written marker here makes
    # paths.read_data_schema_version fall back to "assume current", which
    # silently skips every migration the overlay still needs.
    atomic_write_text(target / ".schema-version", f"{DATA_SCHEMA_VERSION}\n")
    registry = seed_services(target)
    print(f"Initialized data folder at {target} (schema v{DATA_SCHEMA_VERSION}): "
          f"{len(roots)} subject roots ({', '.join(roots)}).")
    print(f"Seeded the data-address registry at {registry} "
          f"from {example_services_path()}.")
    print(f"Its `names:` block is not seeded and does not need to be: the "
          f"engine answers every data-address name from {example_names_path()} "
          f"until this root carries a `names:` block of its own, which then "
          f"wins outright.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Scaffold a HEADING OS private data folder")
    # Beside the MAIN CLONE. This entry point carries no `require_main_clone()`,
    # so run from a worktree the old default would have SCAFFOLDED a second,
    # empty overlay at `.yard/.heading-os/.heading-os-data`.
    ap.add_argument("--path", default=str(
        engine_siblings_root(get_workspace_root()) / ".heading-os-data"))
    args = ap.parse_args()
    return init_data(Path(args.path).expanduser())


if __name__ == "__main__":
    raise SystemExit(main())
