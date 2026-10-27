#!/usr/bin/env python3
"""Version-sync guard.

`pyproject.toml` is the single source of truth for the engine version. This
check asserts two surfaces agree with it, one human-facing and one generated:

  * README.md § Status — the `vX.Y.Z` token under the "## Status" heading.
  * uv.lock           — the `version` of the `[[package]]` entry whose `name`
                        matches `project.name`. Generated, never hand-edited,
                        which is why it drifts unseen.

CHANGELOG.md and ROADMAP.md were two more surfaces until 2026-09-25, when both
left the engine for the private data repository (HOS-131). The operator decided
the same day that the version is read from pyproject.toml only, so their legs
were dropped rather than retargeted: a public clone has neither file to read.

Exit 0 when all agree; exit 1 with a diff-style message otherwise. Wired into
`.pre-commit-config.yaml` (files: README.md|pyproject.toml|uv.lock) and the CI
`guards` job.

The lock is checked because `uv sync` silently rewrites a stale lock version,
so every new worktree would open dirty (drift found and fixed 2026-09-03).

Usage:
    python scripts/check-version-sync.py [--quiet]

Coverage: none live. Two runs held this gate, one of them there because the gate
had never read the lockfile at all. Both were parked in the 2026-09-21
test-suite split, so this file is the gate and nothing is the gate on it.
"""
from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.utils.colors import GREEN, RED, RESET, YELLOW  # noqa: E402
from scripts.utils.workspace import get_workspace_root  # noqa: E402

_SEMVER = r"\d+\.\d+\.\d+"


def _pyproject_version(root: Path) -> str:
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def _pyproject_name(root: Path) -> str:
    """The distribution name, read rather than assumed.

    `uv.lock` lists every dependency, so finding "the" version in it means
    finding OUR entry among them. Hardcoding `heading-os-engine` here would
    make the guard silently stop checking anything the day the project is
    renamed: no entry would match, and a `None` would be read as drift against
    a file nobody had touched. The name comes from the same source of truth as
    the version.
    """
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["name"]


def _readme_status_version(root: Path) -> str | None:
    text = (root / "README.md").read_text(encoding="utf-8")
    # Anchored to line start. Without
    # `^` and MULTILINE, `re.search` matched the substring "## Status" INSIDE a
    # deeper heading - `### Status` contains it from its second `#` - and
    # inside prose that merely names the section. The first such match won, so
    # the guard could compare pyproject against a subsection's stale token and
    # report drift on a correct README, or pass while the real `## Status`
    # heading had drifted. The docstring always scoped this to the `## Status`
    # HEADING; the regex did not.
    # The terminator needs no `^`: it already consumes the newline, and a
    # position just past `\n` IS a line start under MULTILINE, so the anchor
    # there could never change a match. Only the LEADING one does work.
    m = re.search(r"^##\s+Status\b(.*?)(?:\n##\s|\Z)", text,
                  re.DOTALL | re.MULTILINE)
    if not m:
        return None
    v = re.search(rf"`v({_SEMVER})`", m.group(1))
    return v.group(1) if v else None


def _uv_lock_version(root: Path, package: str) -> str | None:
    """The locked version of THIS package inside `uv.lock`.

    Returns None when the lock is absent or carries no entry for `package`.
    Both read as drift at the call site, which is the safe direction: a guard
    whose input collapsed must report rather than pass, or the silence looks
    exactly like agreement.
    """
    path = root / "uv.lock"
    if not path.exists():
        return None
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    for entry in data.get("package", []):
        if entry.get("name") == package:
            return entry.get("version")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Assert version parity across README, uv.lock and pyproject.")
    parser.add_argument("--quiet", action="store_true", help="print only on failure")
    args = parser.parse_args()

    root = get_workspace_root()
    truth = _pyproject_version(root)
    package = _pyproject_name(root)
    readme = _readme_status_version(root)
    uv_lock = _uv_lock_version(root, package)

    problems = []
    if readme != truth:
        problems.append(f"  README.md § Status : {readme!r:>10}  != pyproject {truth!r}")
    if uv_lock != truth:
        problems.append(f"  uv.lock {package}   : {uv_lock!r:>10}  != pyproject {truth!r}")

    if problems:
        print(f"{RED}Version drift (source of truth = pyproject.toml {truth}):{RESET}")
        print("\n".join(problems))
        print(
            f"{YELLOW}Fix: align README § Status to {truth}; re-lock with "
            f"`uv lock` for uv.lock.{RESET}"
        )
        return 1

    if not args.quiet:
        print(f"{GREEN}Version in sync:{RESET} {truth} "
              f"(README, uv.lock, pyproject agree)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
