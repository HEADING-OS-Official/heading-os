#!/usr/bin/env python3
"""Generate the workspace map BY KIND, into the PRIVATE overlay, from the live tree.

## Buckets are kinds

The buckets are KINDS (`family/leaf`), not directories: since the operator's
decision of 2026-09-16 the map describes what a record IS rather than where it
sits, so it reads the same however the tree moves. The output is the registry
name `datastore-map`.

## Why this exists

`.claude/rules/datastore.md` carried a hand-written map of the datastore: a
bullet list of folders and what each holds. It was written on 2026-04-20 and
never regenerated, so by 2026-09-02 it had drifted. MEASURED that day: it named
14 subtrees and omitted three whole top-level directories, roughly 150 files,
one of which is the `personal` directory under the old top-level `datastore`.

A hand-maintained inventory of a tree that grows every week is the defect shape
this repository keeps finding. It falls behind silently, and a stale map is
worse than no map, because it is read with confidence.

## Why the output is PRIVATE and this code is not

`.claude/rules/` resolves `engine`, and the engine repository is public. The
rule file therefore sat in public while describing a private tree. It stayed
clean only because the three directories it omitted are exactly the ones
holding real counterparty names: regenerating it in place would have published
those names on the first run.

So the two halves are split by what they are, which is what
`.claude/rules/classification.md` asks for. This generator is code and ships
public. Its output is data and is written to the registry name
`datastore-map`, in the private overlay. The rule keeps the policy, which is behaviour, and points
here for the inventory.

## What the map carries, and what it deliberately does not

It carries structure: directory names, file counts, the mix of file types, the
routing destination of each subtree, and how much of each subtree is reachable
by search. It does NOT carry file contents. The point is to answer "what is in
here and can I read it", not to duplicate the tree.

## Console-first

`--check` is the non-web verification path: it exits 1 when the map on disk no
longer matches the live tree, so staleness is a failure a gate or a timer can
see, not something a reader has to notice.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.colors import BOLD, GREEN, RED, RESET, YELLOW  # noqa: E402
from scripts.utils.paths import load_env  # noqa: E402
from scripts.utils.data_path import data_path  # noqa: E402
from scripts.utils.workspace import (  # noqa: E402
    get_data_root,
    get_default_tz,
    get_routing_destination,
)
# HOS-240 (2026-10-13): kept, class (d): reads the family manifest to annotate the map with each file's migration row; it places nothing
from scripts.heading_os_2 import resolve  # noqa: E402
from scripts.heading_os_2.manifest import SKIP_DIRS  # noqa: E402
from scripts.heading_os_2.resolve import PER_FILE_FILENAME  # noqa: E402
from scripts.heading_os_2.labels import (  # noqa: E402
    LabelConflict,
    UnknownRoot,
    WalkConfig,
    load_manifest,
    resolve_labels,
)

# Suffixes whose content a reader cannot open directly. Each one needs an
# `-extract.md` companion or it is invisible to the memory index, and therefore
# to `/recall`. This list is the reason the map reports reach at all.
OPAQUE_SUFFIXES = {
    ".pdf", ".xlsx", ".pptx", ".docx", ".dotx", ".xls", ".ppt", ".doc",
    ".zip", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
    ".otf", ".ttf", ".woff", ".woff2", ".eot", ".mp4", ".mp3", ".pcap",
}

# Suffixes a reader and the index can both read as they are.
READABLE_SUFFIXES = {".md", ".txt", ".json", ".jsonl", ".csv", ".yaml",
                     ".yml", ".html", ".css", ".py", ".sh", ".xml"}


def map_path() -> Path:
    """Where the generated map goes. Resolved at call time, never at import.

    The data root, NOT `get_reference_dir()`. That distinction is the
    whole point of this function and it was got wrong once, on 2026-09-02,
    during the very change that split the map out of the public rule.

    `get_reference_dir()` returns the ENGINE root, because `reference/` is
    engine content that ships in the public clone. Calling it here wrote a
    file naming every real datastore directory straight into the public
    repository. It was caught because it was still
    untracked, which is luck, not a control.

    The registry name below resolves inside the private data overlay, which is
    where the other `private`-routed `reference/` files already live.

    Resolved on every call, never at import: a module constant would answer
    once during import, and a caller that repointed the data root afterwards
    would still write the operator's real overlay.

    It can RAISE, and `survey()` is where that
    lands: it calls this inside a `try` that degrades to `mine = None`, so on a
    data root with no registry `--check` would report STALE every run rather
    than compare. That is a named consequence rather than a silent one; the
    remedy is to add the name to the registry, which the refusal says.
    """
    return data_path("datastore-map")


def refuse_if_inside_engine(target: Path) -> None:
    """Refuse to write the map anywhere inside the engine checkout.

    A belt beside `map_path()`'s brace. The map names real directories, the
    engine repository is public, and the failure that put it there was a single
    wrong helper call that no test and no scanner would have caught: the file
    was untracked, so `leak-guard.py` never saw it and the push scan never ran
    on it.

    This asks about the WRITE, not about the environment, so it holds however
    the path was derived.
    """
    engine = Path(__file__).resolve().parent.parent
    try:
        target.resolve().relative_to(engine)
    except ValueError:
        return  # outside the engine, which is the only acceptable answer
    raise SystemExit(
        f"{RED}REFUSING to write the datastore map inside the engine "
        f"checkout: {target}{RESET}\nThe map names real datastore directories "
        "and the engine repository is public. It belongs in the private data "
        "overlay. Check HEADING_OS_DATA and `get_data_root()`."
    )


def _walk(root: Path) -> list[Path]:
    """Every file in the overlay, skipping what the migration also skips.

    The skip set is imported from `heading_os_2.manifest` rather than restated,
    because a map that counts a directory the migration never classifies would
    report an unclassified bucket that is an artifact of two lists disagreeing.
    """
    from scripts.utils.runtime_dir import store_dirname

    store = store_dirname(Path(root))
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.startswith(".memory-index")]
        if os.path.relpath(dirpath, root) == ".":
            dirnames[:] = [d for d in dirnames if d != store]  # the store, by its registry name
        for name in filenames:
            out.append(Path(dirpath) / name)
    return out


def _companion_stems(files: list[Path]) -> set[tuple[Path, str]]:
    """(parent, stem) of every `-extract.md` companion that exists."""
    return {
        (p.parent, p.stem[: -len("-extract")])
        for p in files
        if p.name.endswith("-extract.md")
    }


def survey(root: Path) -> dict:
    """Measure the overlay by KIND. Pure: takes a path, returns numbers.

    Buckets are `family/leaf` kinds, not directories; the module docstring
    says why.

    `resolve_labels` is the workspace's existing answer to "what kind is this
    file", and it is the one the ingest walker already uses: the manifest when
    it names the file, the path's own first segment once the file sits under a
    family root, and the legacy directory map before it moves. Nothing here
    reimplements that.

    A file the labeller cannot place is COUNTED AND NAMED, never dropped. An
    inventory that silently omits what it could not classify is the exact defect
    the hand-written map had, one level down.
    """
    # The map is not part of its own subject. It walks the WHOLE overlay now,
    # and it WRITES into that overlay, so without this the survey counts the
    # previous run's output and `--check` can never pass: MEASURED 2026-09-16 on
    # a two-file scratch overlay, write then check reported STALE immediately,
    # and the map's own `Newest` column named `datastore-map.md`. A checker that
    # always fires is a checker nobody reads, which is the reason `_body()`
    # already strips the timestamp; this is the same obligation one level up.
    try:
        mine = map_path().resolve()
    except Exception as exc:  # noqa: BLE001 - a survey must not depend on the writer
        print(f"{YELLOW}map address unresolved ({type(exc).__name__}: {exc}); "
              f"the survey may count a previous map.{RESET}", file=sys.stderr)
        mine = None
    files = [f for f in _walk(root) if mine is None or f.resolve() != mine]
    companions = _companion_stems(files)
    # The manifest is LOCATED, never joined by hand -- which is the defect
    # family this whole migration is about. `resolve.manifest_dir()` is the
    # workspace's own search order (the env override, then `<data>/_manifest/`,
    # then the phase-0 ledger), and a literal join here found nothing: the
    # tables still live under the migration ledger. Without them every legacy
    # file falls to the labeller's `path` tier, which maps by TOP SEGMENT
    # alone, so all of `datastore/` would read as ONE kind while the approved
    # plan splits it into seven.
    try:
        tables = resolve.manifest_dir(root)
    except Exception as exc:  # noqa: BLE001 - reported, never fatal
        print(f"{YELLOW}no family manifest located ({type(exc).__name__}: "
              f"{exc}); kinds come from the legacy path map, which is coarser "
              f"than the plan.{RESET}", file=sys.stderr)
        tables = None
    manifest = load_manifest(tables / PER_FILE_FILENAME) if tables else None
    if manifest is None:
        print(f"{YELLOW}the per-file table was not readable; kinds come from "
              f"the legacy path map, which is coarser than the plan.{RESET}",
              file=sys.stderr)
    cfg = WalkConfig(root=root, manifest=manifest)

    kinds: dict[str, dict] = {}
    unplaced: list[str] = []
    for path in sorted(files):
        rel = path.relative_to(root).as_posix()
        try:
            family, leaf, label_src = resolve_labels(rel, cfg)
        except (UnknownRoot, LabelConflict) as exc:
            unplaced.append(f"{rel} ({type(exc).__name__})")
            continue
        key = f"{family}/{leaf}" if leaf else f"{family}/(leaf not settled)"
        entry = kinds.setdefault(key, {
            "files": 0, "bytes": 0, "suffixes": Counter(), "sources": Counter(),
            "opaque": 0, "opaque_reachable": 0, "reachable": 0,
            "dirs": set(), "routing": Counter(),
            "newest": None, "newest_mtime": 0.0,
        })
        entry["files"] += 1
        entry["sources"][label_src] += 1
        entry["dirs"].add(str(PurePosixPath(rel).parent))
        entry["routing"][get_routing_destination(rel, tree="data")] += 1
        try:
            stat = path.stat()
        except OSError as exc:
            print(f"{YELLOW}skipping unreadable {rel}: {exc}{RESET}",
                  file=sys.stderr)
            continue
        entry["bytes"] += stat.st_size
        entry["suffixes"][path.suffix.lower() or "(none)"] += 1
        if stat.st_mtime > entry["newest_mtime"]:
            entry["newest_mtime"] = stat.st_mtime
            entry["newest"] = rel

        suffix = path.suffix.lower()
        if suffix in OPAQUE_SUFFIXES:
            entry["opaque"] += 1
            if (path.parent, path.stem) in companions:
                entry["opaque_reachable"] += 1
                entry["reachable"] += 1
        elif suffix in READABLE_SUFFIXES:
            entry["reachable"] += 1

    for entry in kinds.values():
        entry["top_suffixes"] = entry["suffixes"].most_common(6)
        # Every destination this kind's files resolve to, most common first.
        # A list rather than one value: a kind can legitimately span more than
        # one, and picking the plurality is how a map starts lying quietly.
        entry["routings"] = entry["routing"].most_common()
        entry["spread"] = len(entry["dirs"])
        entry["label_src"] = entry["sources"].most_common(1)[0][0]
        for gone in ("suffixes", "newest_mtime", "dirs", "routing", "sources"):
            del entry[gone]

    total = len(files)
    opaque = sum(e["opaque"] for e in kinds.values())
    reachable = sum(e["reachable"] for e in kinds.values())
    opaque_with_companion = sum(e["opaque_reachable"] for e in kinds.values())
    return {
        "generated": datetime.now(get_default_tz()).isoformat(timespec="seconds"),
        "root": str(root),
        "total_files": total,
        "placed_files": total - len(unplaced),
        "unplaced": sorted(unplaced),
        "opaque_files": opaque,
        "opaque_unreachable": opaque - opaque_with_companion,
        "reachable_files": reachable,
        "kinds": dict(sorted(kinds.items())),
    }


def render(data: dict) -> str:
    """The map, as markdown. Deterministic apart from the timestamp."""
    total = data["total_files"]
    reachable = data["reachable_files"]
    pct = (100 * reachable // total) if total else 0
    unplaced = data["unplaced"]

    lines = [
        "# Workspace map by kind (generated, do not edit by hand)",
        "",
        f"Generated {data['generated']} by `scripts/datastore-map.py`.",
        "",
        "This file is written into the PRIVATE data overlay because it names "
        "real directories. The policy that governs the datastore lives in the "
        "public engine at `.claude/rules/datastore.md`; this is the inventory "
        "that rule points at. Regenerate with `python scripts/datastore-map.py`.",
        "",
        "**Buckets are KINDS, not directories, and that is deliberate.** Under "
        "the HEADING OS 2 plan the old top-level `datastore` directory does "
        "not survive: its records fan "
        "out across seven family/leaf destinations and nothing is left under "
        "the old name. A kind is a fact about what a record IS rather than "
        "about where it sits this week, so this map reads the same on both "
        "sides of the move. `Spread` is how many directories currently hold "
        "the kind, which is what tells you whether the move has happened yet.",
        "",
        "Every number here is measured from the tree at generation time. If "
        "the map disagrees with the tree, the tree wins and the map is stale: "
        "`python scripts/datastore-map.py --check` says so and exits 1.",
        "",
        "## Totals",
        "",
        f"- Files: **{total}**, of which **{data['placed_files']}** carry a kind",
        f"- Readable without extraction: **{reachable}** ({pct}%)",
        f"- Binary files: **{data['opaque_files']}**, of which "
        f"**{data['opaque_unreachable']}** have no `-extract.md` companion and "
        "are therefore invisible to search",
        f"- Not placed in any kind: **{len(unplaced)}**"
        + (" (listed at the end)" if unplaced else ""),
        "",
        "## Kinds",
        "",
        "| Kind | Routing | Files | Readable | Opaque | Spread | Named by | Main types | Newest |",
        "|---|---|---:|---:|---:|---:|---|---|---|",
    ]
    for name, entry in data["kinds"].items():
        types = ", ".join(f"{suf.lstrip('.')} {n}"
                          for suf, n in entry["top_suffixes"])
        routing = ", ".join(f"{dest} {n}" for dest, n in entry["routings"])
        newest = entry["newest"] or ""
        if len(newest) > 44:
            newest = newest[:41] + "..."
        lines.append(
            f"| `{name}` | {routing} | {entry['files']} | "
            f"{entry['reachable']} | {entry['opaque']} | {entry['spread']} | "
            f"{entry['label_src']} | {types} | `{newest}` |"
        )

    if unplaced:
        lines += [
            "",
            "## Not placed in any kind",
            "",
            "Counted here rather than dropped, and this is not an error "
            "list. Two things put a file here, and they are different "
            "questions. `UnknownRoot`: no manifest row names the file and its "
            "top segment is neither a family root nor a legacy directory the "
            "labeller knows, so nothing in the workspace has said where it "
            "belongs. `LabelConflict`: a manifest row and the family root the "
            "file already sits under disagree, and the path wins because the "
            "row is what PUT the file there and is therefore the stale half. "
            "The tally below says which, and the top-segment counts beside it "
            "are what to triage by. Note that a manifest row naming a family "
            "and leaving the kind empty does NOT land here: that file is "
            "placed, under `family/(leaf not settled)`, which is the "
            "migration's own way of saying a judgement is still open. "
            "Before the manifest was being read at all this number was 102, "
            "because every unplaced file was silently given its directory's "
            "family instead.",
            "",
        ]
        # The list is alphabetical and capped at 60, so it opens on dotfiles and
        # says nothing about the shape of the other 366. The tally is what a
        # reader actually needs: which QUESTION each unplaced file represents,
        # and how many of each there are. Counted from the same strings that are
        # listed below, so the two cannot disagree.
        reasons = Counter(u.rsplit("(", 1)[-1].rstrip(")") for u in unplaced)
        tops = Counter(u.rsplit(" (", 1)[0].split("/")[0] for u in unplaced)
        lines += [
            "By reason: "
            + ", ".join(f"**{n}** {name}" for name, n in reasons.most_common())
            + ". By top segment: "
            + ", ".join(f"`{seg}` {n}" for seg, n in tops.most_common(8))
            + (f", and {len(tops) - 8} other segment(s)" if len(tops) > 8 else "")
            + ".",
            "",
        ]
        lines += [f"- `{u}`" for u in unplaced[:60]]
        if len(unplaced) > 60:
            lines.append(f"- ... and {len(unplaced) - 60} more")

    lines += [
        "",
        "## How to use this",
        "",
        "- **What is new**: `python scripts/datastore-log.py summary` reads "
        "git and answers what appeared, changed or vanished. This map answers "
        "what is here now; that tool answers what moved.",
        "- **Reaching a binary**: a PDF, spreadsheet or deck is invisible to "
        "search until `python scripts/datastore-extract.py` writes its "
        "`-extract.md` companion. The Opaque column counts the ones that still "
        "have none.",
        "- **Routing**: `private` never leaves the data overlay and is never "
        "copied into the public engine repository, including into an example "
        "or a test fixture.",
        "- **Named by**: `manifest` means the migration's own table placed the "
        "file, `vocabulary` that it already sits under its family root, and "
        "`path` that it is still in a legacy directory. A kind whose files "
        "read `path` has not moved yet.",
        "",
    ]
    return "\n".join(lines)


def _body(text: str) -> str:
    """The map without its generation timestamp.

    `--check` compares content, not the clock. Including the timestamp would
    make every run report drift, which is a checker that always fires and is
    therefore a checker nobody reads.
    """
    return "\n".join(
        line for line in text.splitlines()
        if not line.startswith("Generated ")
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate the workspace map by kind into the private overlay.")
    parser.add_argument("--check", action="store_true",
                        help="exit 1 if the map on disk is stale; write nothing")
    parser.add_argument("--json", action="store_true",
                        help="print the survey as JSON on stdout")
    parser.add_argument("--stdout", action="store_true",
                        help="print the map instead of writing it")
    args = parser.parse_args()

    # BEFORE anything reads a clock. `HEADING_OS_TZ` lives in the gitignored
    # `.env` and is exported by nothing, so under systemd `get_default_tz()`
    # answers UTC unless this runs first. The generation stamp would then be
    # dated in UTC while the unit fires at 03:20 local, and around midnight the
    # two disagree by a day. A timer-timezone run caught exactly that here, the
    # fourth time this workspace shipped the defect; it was parked in the
    # 2026-09-21 test-suite split, so a fifth would now go unnoticed. Keep the
    # call first.
    load_env(Path(__file__).resolve().parent.parent)

    # The WHOLE overlay since 2026-09-16, not `get_datastore_dir()`. That
    # helper has no post-move answer to give: `datastore/` fans out into seven
    # kinds and nothing stays under the name, so there is nothing for it to be
    # repointed at. The subject of the map is now the overlay and the buckets
    # are kinds, which is what `survey()` explains at length.
    root = get_data_root()
    if not root.is_dir():
        print(f"{RED}no data overlay at {root}. Nothing to map.{RESET}",
              file=sys.stderr)
        return 1

    data = survey(root)
    if data["total_files"] == 0:
        print(f"{RED}{root} holds no files. A map of an empty corpus is "
              f"not a map, and this is what a wrong data root looks like from "
              f"the outside.{RESET}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(data, indent=2, default=str))
        return 0

    text = render(data)
    if args.stdout:
        print(text)
        return 0

    target = map_path()
    refuse_if_inside_engine(target)

    # A read-only mirror must not be written, and `--check` is a read, so the
    # gate sits between them. A pull-only service host pulls the data repo with
    # `git pull --ff-only` and nothing there ever commits, so ONE local write
    # aborts every later pull. That happened on 2026-08-30: five CRM cards were
    # rewritten as a side effect of a send, the mirror sat five commits behind
    # for three and a half days, and systemd reported SUCCESS throughout.
    # The map (registry name `datastore-map`) is tracked, so writing it would wedge
    # the mirror the same way.
    if not args.check and os.environ.get("HEADING_OS_DATA_READONLY"):
        print(f"{YELLOW}HEADING_OS_DATA_READONLY is set: this host mirrors the "
              f"data repository and never writes to it. Skipping the map "
              f"write.{RESET} Run the generator on the operator's own "
              f"workstation and let the mirror pull the result.", file=sys.stderr)
        return 0

    if args.check:
        if not target.exists():
            print(f"{RED}no map at {target}. Run "
                  f"`python scripts/datastore-map.py` to write it.{RESET}",
                  file=sys.stderr)
            return 1
        try:
            current = target.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"{RED}cannot read {target}: {exc}{RESET}", file=sys.stderr)
            return 1
        if _body(current) != _body(text):
            print(f"{RED}the workspace map is STALE.{RESET} {target}",
                  file=sys.stderr)
            print(f"{YELLOW}Regenerate with `python "
                  f"scripts/datastore-map.py`.{RESET}", file=sys.stderr)
            return 1
        print(f"{GREEN}map is current{RESET} ({data['total_files']} files)")
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    # Atomic: a reader must never see a half-written map, and a crash must
    # never leave one on disk.
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(target)

    pct = 100 * data["reachable_files"] // data["total_files"]
    print(f"{GREEN}wrote{RESET} {target}")
    print(f"  {BOLD}{data['total_files']}{RESET} files, "
          f"{BOLD}{data['reachable_files']}{RESET} readable without extraction "
          f"({pct}%), "
          f"{BOLD}{len(data['kinds'])}{RESET} kinds"
          + (f", {BOLD}{len(data['unplaced'])}{RESET} file(s) placed in none"
             if data["unplaced"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
