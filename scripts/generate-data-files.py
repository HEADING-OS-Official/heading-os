#!/usr/bin/env python3
"""Write the data-repository files that cannot read a registry name, FROM the registry.

    python scripts/generate-data-files.py --check              # drift? exit 1, print the diff
    python scripts/generate-data-files.py --out <dir>          # write the results under <dir>
    python scripts/generate-data-files.py --write              # write in place (HELM only)

HOS-234, operator decision 2026-10-13: "NO hardcoded paths" (translated). Moving a data
artifact is an edit of the data registry and nothing else. Code reads the
registry through `data_path()`, but some files are read by programs that cannot
call a resolver: git reads the data repository's `.gitignore`, and
`scripts/hindsight-sync.py` reads its wave lists as plain prefixes. Those files
carry the addresses as text, so a move left them pointing at the old place.

Each such file gets a GENERATED BLOCK, between two marker comments, holding the
addresses of one registry group (`groups:` in the registry), rendered relative
to the data root: a directory as `rel/`, a file as `rel`, and for `.gitignore`
anchored with a leading `/`. Everything outside the block stays hand-written,
because these files carry the operator's own comments and rules nobody has named
yet. A hand-written line that says exactly what a generated one says is a
duplicate: `--check` reports it and a write removes it. The remaining
hand-written address lines are counted, never judged: naming them is how they
shrink.

The targets are the table `TARGETS` below. A target whose group the registry
does not define is reported and left alone; a target whose file does not exist
on this tree (a public clone) is skipped and said.

Writing in place changes what a running timer reads, so it happens in HELM only
(`clone_guard.require_main_clone`) and never on a read-only host. From a yard,
`--check` and `--out` are the two modes there are.
"""
from __future__ import annotations

import argparse
import difflib
import os
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.data_path import (DataPathError, data_path, group_names,  # noqa: E402
                                     load_groups, store_dirname)
from scripts.utils.registry_bootstrap import required_ignore_lines  # noqa: E402
from scripts.utils.workspace import get_data_root  # noqa: E402

BEGIN = ("# >>> GENERATED from the data registry group {group!r} by "
         "scripts/generate-data-files.py. Edit the registry, not these lines.")
END = "# <<< end of the generated block"


@dataclass(frozen=True)
class Target:
    key: str               # what --check prints and --out names the file
    group: str             # the registry group whose addresses fill the block
    gitignore: bool        # anchor with `/`, and add the runtime store's lines
    locate: object         # data_root -> the file's path

    def path(self, root: Path) -> Path:
        return self.locate(root)


def _wave(name: str):
    # The four file names are `scripts/hindsight-sync.py`'s (`core_prefixes()`
    # and its siblings), beside the ledger directory the registry places.
    return lambda root: data_path("runtime:hindsight-sync/state", data_root=root) / name


TARGETS: tuple[Target, ...] = (
    # `.gitignore` at the data root is git's own contract, not a data address.
    Target("gitignore", "git-ignored", True, lambda root: root / ".gitignore"),
    Target("wave-core", "hindsight-wave-core", False, _wave("wave-core.txt")),
    Target("wave-skip", "hindsight-wave-skip", False, _wave("wave-skip.txt")),
    Target("wave-admit", "hindsight-wave-admit", False, _wave("wave-admit.txt")),
    Target("wave-last", "hindsight-wave-last", False, _wave("wave-last.txt")),
)


def _render(name: str, root: Path, gitignore: bool) -> str:
    """One group member as a line: root-relative, `/`-suffixed when a directory."""
    path = data_path(name, data_root=root)  # leak-guard: ok (a member of a registry group; the registry lists the names)
    rel = PurePosixPath(path.resolve().relative_to(root.resolve()).as_posix())
    is_dir = path.is_dir() or (not path.exists() and not rel.suffix)
    line = f"{rel}/" if is_dir else str(rel)
    return f"/{line}" if gitignore else line


def generated_lines(target: Target, root: Path) -> list[str] | None:
    """The block's lines, or None when the registry defines no such group."""
    if target.group not in load_groups(root):
        return None
    lines: list[str] = []
    if target.gitignore:
        lines += [f"/{line}" for line in required_ignore_lines(store_dirname(root))]
    for name in group_names(target.group, data_root=root):
        lines.append(_render(name, root, target.gitignore))
    return list(dict.fromkeys(lines))


def _key(line: str, gitignore: bool) -> str:
    line = line.strip()
    return line.lstrip("/") if gitignore else line


def regenerate(text: str, target: Target, block: list[str]) -> tuple[str, list[str], int]:
    """`text` with its generated block replaced (or appended) and hand duplicates removed.

    Returns the new text, the duplicates removed, and how many hand-written
    address lines remain outside the block.
    """
    lines = text.splitlines()
    begin = BEGIN.format(group=target.group)
    try:
        start = lines.index(begin)
        stop = lines.index(END, start)
        before, after = lines[:start], lines[stop + 1:]
    except ValueError:
        before, after = lines, []
    wanted = {_key(line, target.gitignore) for line in block}
    kept, dropped = [], []
    for line in before + [None] + after:
        if line is not None and line.strip() and not line.lstrip().startswith("#") \
                and _key(line, target.gitignore) in wanted:
            dropped.append(line)
        else:
            kept.append(line)
    cut = kept.index(None)
    head, tail = kept[:cut], kept[cut + 1:]
    if not tail:
        while head and not head[-1].strip():
            head.pop()
        head.append("")
    body = head + [begin, *block, END] + tail
    hand = sum(1 for line in head + tail
               if line.strip() and not line.lstrip().startswith(("#", "!")))
    return "\n".join(body) + "\n", dropped, hand


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true",
                      help="exit 1 and print the diff when any file has drifted")
    mode.add_argument("--out", type=Path, metavar="DIR",
                      help="write each regenerated file to DIR/<target>, not in place")
    mode.add_argument("--write", action="store_true",
                      help="write in place (HELM only; changes what running timers read)")
    parser.add_argument("--data-root", type=Path, default=None)
    args = parser.parse_args(argv)

    if args.write:
        from scripts.utils.clone_guard import require_main_clone
        from scripts.utils.crm_autolog import data_is_readonly
        require_main_clone(__file__)
        if data_is_readonly():
            print("REFUSED: this host's data clone is read-only", file=sys.stderr)
            return 2

    root = (args.data_root or get_data_root()).resolve()
    drift = False
    for target in TARGETS:
        try:
            path = target.path(root)
            block = generated_lines(target, root)
        except DataPathError as exc:
            print(f"{target.key}: REFUSED: {exc}", file=sys.stderr)
            drift = True
            continue
        if block is None:
            print(f"{target.key}: the registry defines no group {target.group!r}; "
                  f"nothing generated, the file stays hand-written")
            continue
        if not path.is_file():
            print(f"{target.key}: {path} does not exist on this tree; skipped")
            continue
        old = path.read_text(encoding="utf-8")
        new, dropped, hand = regenerate(old, target, block)
        state = "clean" if new == old else "DRIFT"
        print(f"{target.key}: {state}; {len(block)} generated line(s) from {target.group!r}, "
              f"{len(dropped)} hand duplicate(s), {hand} hand-written line(s) outside the block")
        if new == old:
            continue
        if args.check:
            drift = True
            sys.stdout.writelines(difflib.unified_diff(
                old.splitlines(keepends=True), new.splitlines(keepends=True),
                f"{path} (now)", f"{path} (from the registry)"))
        elif args.out is not None:
            args.out.mkdir(parents=True, exist_ok=True)
            (args.out / target.key).write_text(new, encoding="utf-8")
            print(f"  written to {args.out / target.key}")
        else:
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            tmp.write_text(new, encoding="utf-8")
            os.chmod(tmp, path.stat().st_mode & 0o777)
            os.replace(tmp, path)
            print(f"  written in place: {path}")
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
