#!/usr/bin/env python3
"""Derive the HEADING OS 2 layer globs for config/memory-index.yaml from a plan.

Usage:
    .venv/bin/python scripts/dev/derive-memory-index-globs.py --plan <plan.tsv>
    .venv/bin/python scripts/dev/derive-memory-index-globs.py --plan <plan.tsv> --check

The move puts every file at `<family>/<leaf>/<path today>`, so a layer whose
glob claims `crm/contacts/*.md` today must claim  # leak-guard: ok (migration vocabulary: the pre-move to post-move spelling this tool derives)
`status/entity/crm/contacts/*.md` tomorrow, and nothing else new. Which  # leak-guard: ok (migration vocabulary: the pre-move to post-move spelling this tool derives)
`<family>/<leaf>` prefixes a layer needs is a fact about the PLAN, not about
anyone's memory of the tree: for each plan row, resolve the source against
today's config (first glob to match wins, exactly as the build's walk claims),
collect the row's `family/leaf` under that layer, and prepend every collected
prefix to the layer's glob as a brace alternative, keeping the bare glob so the
old address still resolves until the move has run.

Why keeping the old glob is safe under a first-match resolver: a dest is
`<f>/<l>/<src>`, and `<f>/<l>/<G1>` matches it exactly when `G1` matches
`<src>`, so an earlier layer's new glob can capture a later layer's dest only
if its old glob already captured that later layer's source, which the old
config's first-match order already decided the same way.

Only the DATA store's layers are derived. The `code` collection builds from
the ENGINE root and never indexes an overlay file, even one that lives under
an overlay `.claude/` directory; a `skill`/`rule` row in the plan is reported
and left alone.

`--check` exits 1 when the config on disk differs from the derivation, which
is how a plan that changed after the config was written gets noticed.

THE BARE ALTERNATIVE IS NOT UNCONDITIONAL, since 2026-09-19. Keeping it was
right while the move was pending and is a defect once the move has run: a dest
is `<f>/<l>/<src>`, so the bare alternative and any prefixed one claim the SAME
record at its two addresses, and a record that exists at both is indexed twice
and answers one query as two documents. Operator instruction that day: "there
cannot be a file lying at both addresses." (translated)

So the bare alternative is kept only where the pre-move address STILL HOLDS
FILES, and that is measured against the live overlay rather than argued from the
plan -- the plan predicts a move, and cannot say what the source address holds
after it ran. MEASURED 2026-09-19 over the operator's overlay: of the eleven
layers carrying a prefix group, ten bare alternatives matched zero files and one
(`outputs`) matched 71, all of them under `outputs/operations/**` (69, chiefly  # leak-guard: ok (history: MEASURED 2026-09-19)
the migration's own journal, which the operator ruled STAYS) and
`outputs/deliverables/**` (2). Without a `data_root` nothing is measured and  # leak-guard: ok (history: MEASURED 2026-09-19)
every bare alternative is kept, which is the conservative answer for a clone
that cannot see the overlay.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import sys
from collections import defaultdict
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

# HOS-240 (2026-10-13): kept, class (d): a dev tool that derives index globs; history of the move
from scripts.heading_os_2.families import is_valid  # noqa: E402
from scripts.utils.workspace import (  # noqa: E402
    data_overlay_present, get_data_root, get_workspace_root)

CONFIG_REL = "config/memory-index.yaml"


def _memory_index_module():
    """`scripts/memory-index.py` has a hyphen, so it is loaded by path. The two
    helpers taken from it (`layer_matchers`, `layer_for_path`) are the relocate
    subcommand's own resolver, which agrees with the build's walk by test."""
    root = get_workspace_root()
    spec = importlib.util.spec_from_file_location(
        "memory_index_for_globs", root / "scripts" / "memory-index.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def data_layers(cfg: dict) -> list[str]:
    """The DATA store's glob layers, in config order (every non-`code`
    collection; a config with no collections map is all-data)."""
    colls = cfg.get("collections") or {}
    if colls:
        members = set()
        for name, lyrs in colls.items():
            if name != "code":
                members.update(lyrs)
    else:
        members = {lc["layer"] for lc in cfg.get("layers", [])}
    # A registry-named layer (`names:`/`files:`) follows the registry and has
    # no prefix group for this derivation to compute.
    return [lc["layer"] for lc in cfg.get("layers", [])
            if lc["layer"] in members and lc.get("glob") and not lc.get("source")
            and not any(lc.get(k) for k in ("names", "groups", "files", "file_groups", "literal"))]


def bare_glob(glob: str) -> str:
    """The glob without its leading prefix group, i.e. what the layer claimed
    before this derivation touched it.

    Both shapes this file can emit are stripped: `{,a/b/}rest` (the bare address
    kept) and `{a/b/}rest` (dropped). The test is that EVERY option of the
    leading group is empty or ends with `/`, which is what a prefix group is; a
    leaf-set group such as `{principles,positions}` is left alone, and so is a
    brace that does not start the pattern.

    Stripping the second shape too is what keeps the derivation idempotent. Until
    2026-09-19 only `{,` was recognised, because only `{,` could be produced; a
    config carrying `{status/entity/}crm/contacts/*.md` would have re-derived as  # leak-guard: ok (history: the shape recognised from 2026-09-19)
    if that whole string were the layer's pre-move address, and the plan's source
    rows would then have matched no layer and the prefix been lost.
    """
    if not glob.startswith("{"):
        return glob
    close = glob.index("}")
    options = glob[1:close].split(",")
    if not all(o == "" or o.endswith("/") for o in options):
        return glob
    return glob[close + 1:]


def with_prefixes(glob: str, prefixes, *, keep_bare: bool = True) -> str:
    """The layer's glob carrying its `<family>/<leaf>/` prefixes.

    `keep_bare` False drops the empty first alternative, so the pre-move address
    is no longer claimed. The brace is written even for a single prefix, never
    flattened into a plain path: `bare_glob` recovers the layer's pre-move glob
    by stripping that group, and a flattened form would leave it nothing to
    strip and make the next derivation resolve the plan against the wrong
    address.
    """
    prefixes = sorted(set(prefixes))
    if not prefixes:
        return glob
    lead = "{," if keep_bare else "{"
    return lead + ",".join(p + "/" for p in prefixes) + "}" + glob


def bare_matches(bare: str, data_root: Path, mod) -> int:
    """How many files the layer's PRE-MOVE address still holds, today.

    Brace expansion is `memory-index.py`'s own `expand_brace_glob` and the walk
    is the build's own `root.glob(sub)`, so this counts what the indexer would
    count and not an imitation of it.
    """
    total = 0
    for sub in mod.expand_brace_glob(bare):
        total += sum(1 for p in data_root.glob(sub) if p.is_file())
    return total


def read_plan(plan: Path) -> list[dict]:
    with plan.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    for row in rows:
        if row["source"] == row["dest"]:
            # A no-op row (MEASURED 2026-09-14: `status/README.md` lists  # leak-guard: ok (history: MEASURED 2026-09-14)
            # itself with leaf `README`); it moves nowhere and names no leaf.
            continue
        if not is_valid(row["family"], row["leaf"]):
            raise SystemExit(
                f"{plan}: {row['source']} names {row['family']}/{row['leaf']}, "
                f"which the families vocabulary does not define")
    return rows


def derive(cfg: dict, rows: list[dict], mod,
           data_root: Path | None = None) -> tuple[dict[str, str], dict]:
    """(layer -> new glob) plus a report. Resolution uses the BARE globs, so a
    config that already carries prefixes derives the same answer again.

    `data_root` is the live overlay. When it is given, a layer keeps its bare
    (pre-move) alternative only while that address still holds files; when it is
    None nothing is measured and every bare alternative is kept. See the module
    docstring for why this is a measurement and not a reading of the plan.
    """
    layers = data_layers(cfg)
    bare_cfg = {"layers": [
        {**lc, "glob": bare_glob(lc["glob"])} if lc.get("glob") else lc
        for lc in cfg["layers"]
        if not any(lc.get(k) for k in ("names", "groups", "files", "file_groups", "literal"))]}
    matchers = mod.layer_matchers(bare_cfg, set(layers))
    prefixes: dict[str, set[str]] = defaultdict(set)
    unindexed = 0
    outside = defaultdict(int)   # rows whose source resolves to a non-data layer
    code_matchers = mod.layer_matchers(
        bare_cfg, {lc["layer"] for lc in cfg["layers"]} - set(layers))
    for row in rows:
        src = row["source"]
        if src == row["dest"]:
            continue
        layer = mod.layer_for_path(src, matchers)
        if layer is None:
            other = mod.layer_for_path(src, code_matchers)
            if other:
                outside[other] += 1
            else:
                unindexed += 1
            continue
        prefixes[layer].add(f"{row['family']}/{row['leaf']}")
    globs = {}
    bare_live: dict[str, int] = {}
    for lc in cfg["layers"]:
        if lc["layer"] not in layers:
            continue
        bare = bare_glob(lc["glob"])
        keep_bare = True
        if data_root is not None and prefixes[lc["layer"]]:
            bare_live[lc["layer"]] = bare_matches(bare, Path(data_root), mod)
            keep_bare = bare_live[lc["layer"]] > 0
        globs[lc["layer"]] = with_prefixes(bare, prefixes[lc["layer"]],
                                           keep_bare=keep_bare)
    report = {"rows": len(rows), "unindexed": unindexed, "outside": dict(outside),
              "prefixes": {k: sorted(v) for k, v in prefixes.items()},
              "bare_live": bare_live}
    return globs, report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--plan", required=True, type=Path)
    ap.add_argument("--config", type=Path, default=get_workspace_root() / CONFIG_REL)
    ap.add_argument("--check", action="store_true",
                    help="exit 1 when the config's globs differ from the derivation")
    ap.add_argument("--no-overlay", action="store_true",
                    help="derive without measuring the live overlay, so every "
                         "bare (pre-move) alternative is kept")
    args = ap.parse_args()
    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    mod = _memory_index_module()
    data_root = None
    if not args.no_overlay and data_overlay_present():
        data_root = get_data_root()
    globs, report = derive(cfg, read_plan(args.plan), mod, data_root=data_root)
    current = {lc["layer"]: lc.get("glob") for lc in cfg["layers"]}
    drift = {k: v for k, v in globs.items() if current.get(k) != v}
    print(f"plan rows {report['rows']}; {report['unindexed']} never indexed; "
          f"outside the data store: {report['outside'] or 'none'}")
    if data_root is None:
        print("no overlay measured: every bare (pre-move) alternative is kept")
    else:
        live = {k: v for k, v in report["bare_live"].items() if v}
        print(f"bare (pre-move) addresses still holding files: {live or 'none'}")
    for layer, glob in globs.items():
        mark = "  " if layer not in drift else "! "
        print(f"{mark}{layer:18} {glob}")
    if args.check:
        if drift:
            print(f"{len(drift)} layer(s) differ from the derivation (marked !)")
            return 1
        print("config matches the derivation")
    return 0


if __name__ == "__main__":
    sys.exit(main())
