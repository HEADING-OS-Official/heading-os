#!/usr/bin/env python3
"""Read-only check: every git-tracked Markdown file whose frontmatter is broken.

`scripts.utils.markdown.parse_frontmatter` falls back to a partial regex reading
when a header is not valid YAML (kept by the 2026-10-15 engine-audit cleanup
map, row 5-P3-17) and since HOS-361 logs a warning naming the file. That warning
fires only when some caller happens to read the file; this check lists every
such file up front, over two corpora:

  data    `git ls-files '*.md'` in the data repository (`get_data_root()`)
  skills  `git ls-files '.claude/skills/*/SKILL.md'` in the engine checkout

Each file is classified with `parse_frontmatter_strict`. A file with no opening
fence is not a finding (most notes have no frontmatter); every other failure
kind is (`no-closing-fence`, `invalid-yaml`, `empty`, `not-a-mapping`), and so
is a file that cannot be read as UTF-8 (`unreadable`).

Output: one tab-separated line per finding on stdout,
`<corpus> <kind> <path relative to that corpus root> <detail>`, or with
`--json` one object `{"scanned": M, "findings": [...]}`; then a summary line on
stderr in both modes.

Exit 0: no findings. Exit 1: findings. Exit 2, decided before any scan, with
nothing on stdout: the data root is the demo tree, the data root cannot be
resolved, or a `git ls-files` failed.

Writes nothing, anywhere. Runs `git ls-files` only, which reads the index.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.markdown import (  # noqa: E402
    FM_NO_OPENING,
    FM_OK,
    parse_frontmatter_strict,
)
from scripts.utils.workspace import (  # noqa: E402
    DataRootError,
    data_root_is_demo,
    get_data_root,
    get_workspace_root,
)

PROG = "check-frontmatter"


class ListingError(Exception):
    """A `git ls-files` for one corpus did not succeed."""


def _ls_files(corpus: str, root: Path, pattern: str) -> list[str]:
    """Tracked paths under ``root`` matching ``pattern``, sorted."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--", pattern],
            capture_output=True, check=False)
    except OSError as exc:
        raise ListingError(f"git ls-files for corpus {corpus} could not run "
                           f"({type(exc).__name__}: {exc})") from exc
    if proc.returncode != 0:
        err = os.fsdecode(proc.stderr).strip().splitlines()
        raise ListingError(f"git ls-files for corpus {corpus} exited "
                           f"{proc.returncode}" + (f": {err[0]}" if err else ""))
    return sorted(os.fsdecode(p) for p in proc.stdout.split(b"\0") if p)


def _first_line(text: str) -> str:
    return text.split("\n", 1)[0] if text else ""


def _classify(path: Path) -> tuple[str, str] | None:
    """``(kind, detail)`` for a finding, None for a file that is fine."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return "unreadable", type(exc).__name__
    _data, kind, detail = parse_frontmatter_strict(text)
    if kind in (FM_OK, FM_NO_OPENING):
        return None
    return kind, _first_line(detail)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="List git-tracked Markdown files whose frontmatter is broken. Read-only.")
    parser.add_argument("--json", action="store_true",
                        help="print one JSON object on stdout instead of tab-separated lines")
    args = parser.parse_args(argv)

    # Every exit-2 condition is decided here, before anything is scanned or printed.
    # The resolve comes first because `data_root_is_demo()` calls `get_data_root()`
    # and raises the same DataRootError when the root cannot be resolved.
    try:
        data_root = get_data_root()
        demo = data_root_is_demo()
    except DataRootError as exc:
        print(f"{PROG}: the data root cannot be resolved ({exc})", file=sys.stderr)
        return 2
    if demo:
        print(f"{PROG}: the data root is the demo tree; no data repository to check",
              file=sys.stderr)
        return 2
    engine_root = get_workspace_root()
    try:
        corpora = [
            ("data", data_root, _ls_files("data", data_root, "*.md")),
            ("skills", engine_root,
             _ls_files("skills", engine_root, ".claude/skills/*/SKILL.md")),
        ]
    except ListingError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 2

    scanned = 0
    findings: list[dict] = []
    for corpus, root, rels in corpora:
        for rel in rels:
            scanned += 1
            result = _classify(root / rel)
            if result is not None:
                kind, detail = result
                findings.append({"corpus": corpus, "kind": kind,
                                 "path": rel, "detail": detail})

    if args.json:
        print(json.dumps({"scanned": scanned, "findings": findings},
                         ensure_ascii=False, indent=2))
    else:
        for f in findings:
            print(f"{f['corpus']}\t{f['kind']}\t{f['path']}\t{f['detail']}")
    print(f"{PROG}: {len(findings)} finding(s) in {scanned} file(s) scanned",
          file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
