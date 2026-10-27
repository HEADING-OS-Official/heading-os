"""What the engine is: `config/engine-allowlist.yaml`, matched in ONE place.

Two readers ask this question and must get one answer. `scripts/check-engine-only.py`
judges git's index against the allowlist at commit and push; the routing
resolver in `scripts/utils/workspace.py` answers `engine` for a path only when
the allowlist covers it (HOS-232). A second copy of the pattern matcher is how
the two would drift, so the matcher, the NEVER list and the loader live here
and both import them.

The allowlist is the operator's definition (HOS-131, 2026-09-25): the engine is
code and what that code cannot run or install without; everything else is a
leak. `covers()` therefore answers for ANY path, tracked or not, existing or
not: a class pattern matches by shape, and a per-file entry matches its exact
path. Whether an entry names a tracked file is the commit gate's question
(`stale-allow`), not this one's.

Fails closed: an absent, unreadable or malformed allowlist covers nothing.
"""
from __future__ import annotations

import functools
import re
from pathlib import Path

ALLOWLIST_REL = "config/engine-allowlist.yaml"

#: Content classes no allowlist entry may cover. The one docs/ page is the
#: install guide, which the operator kept; everything else under docs/ is
#: documentation, which he did not.
NEVER: tuple[str, ...] = (
    "examples/**",
    "records/**",
    "docs/**",
    "**/brands/**",
    "**/*.png", "**/*.jpg", "**/*.jpeg", "**/*.gif", "**/*.webp", "**/*.svg",
    "**/*.pptx", "**/*.docx", "**/*.dotx", "**/*.xlsx", "**/*.pdf",
    "**/*.ttf", "**/*.otf", "**/*.woff", "**/*.woff2",
)
NEVER_EXCEPT: frozenset[str] = frozenset({
    "docs/AGENTVIEW.md",
    "docs/CLIPROXY.md",
    "docs/HERDR.md",
    "docs/HINDSIGHT.md",
    "docs/INSTALL.md",
    "docs/LIFIC.md",
    "docs/RULES-AND-PROTECTIONS.md",
    "docs/SKILLS-COMMUNICATION.md",
    "docs/SKILLS-CONTENT.md",
    "docs/SKILLS-DAILY.md",
    "docs/SKILLS-INTEL.md",
    "docs/SKILLS-STRATEGY.md",
    "docs/SKILLS-SYSTEM.md",
    "docs/SKILLS.md",
    "docs/WHATS-NEW.md",
    "docs/WORKING-WITH-HEADING-OS.md",
    "docs/YARDS.md",
    "docs/assets/how-it-works.svg",
    "docs/assets/install-steps.svg",
    "docs/assets/security.svg",
})


@functools.lru_cache(maxsize=None)
def _regex(pattern: str) -> re.Pattern:
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def matches(pattern: str, rel: str) -> bool:
    return _regex(pattern).match(rel) is not None


def is_never(rel: str) -> bool:
    return rel not in NEVER_EXCEPT and any(matches(p, rel) for p in NEVER)


@functools.lru_cache(maxsize=4)
def _load_cached(path: str, mtime_ns: int, size: int) -> tuple[tuple[str, ...], frozenset[str]] | None:
    """(class patterns, per-file paths), or None when the file cannot be trusted.

    Keyed on file identity, like the routing map, so a long-running process sees
    an edited allowlist on its next call.
    """
    import yaml

    from scripts.utils import yamlio

    try:
        with open(path, encoding="utf-8") as fh:
            data = yamlio.safe_load(fh) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    classes, entries = data.get("classes") or [], data.get("files") or []
    if not isinstance(classes, list) or not isinstance(entries, list):
        return None
    patterns = tuple(p for c in classes if isinstance(c, dict)
                     for p in (c.get("patterns") or []) if isinstance(p, str))
    files = frozenset(e["path"] for e in entries
                      if isinstance(e, dict) and isinstance(e.get("path"), str))
    if not patterns and not files:
        return None
    return patterns, files


def load(root: Path) -> tuple[tuple[str, ...], frozenset[str]] | None:
    """The allowlist under engine checkout `root`, or None (covers nothing)."""
    path = Path(root) / ALLOWLIST_REL
    try:
        st = path.stat()
    except OSError:
        return None
    return _load_cached(str(path), st.st_mtime_ns, st.st_size)


def covers(root: Path, rel: str) -> bool | None:
    """True when the allowlist under `root` makes `rel` engine; None when there is
    no usable allowlist, which every caller must read as NOT covered."""
    loaded = load(root)
    if loaded is None:
        return None
    patterns, files = loaded
    if is_never(rel):
        return False
    return rel in files or any(matches(p, rel) for p in patterns)
