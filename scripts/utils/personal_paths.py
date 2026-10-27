"""Does this text name a CEO-only thread? One answer, several callers.

Extracted from `.claude/hooks/_dispatch.py` on 2026-09-11, when a second caller
appeared. `check_protect_personal_threads` refuses a tool call that would move
CEO-only material.

The extraction is the point rather than a tidy-up. This repository's dominant
defect shape is a fix that landed in one of N copies
(`.claude/rules/development-standards.md`, obligation 3), and a second regex
for "is this a personal thread" in a second file is the same defect waiting:
the archive shape below was added to the wall's Bash patterns years before the
read patterns learned it, and for that whole time `cp` of an archived CEO-only
file was refused while `cat` of it went through.

`_dispatch.py` keeps `PERSONAL_PATH_RE` as its own name for the stricter
file-inside-the-directory form, which is a different question and has a
different answer.
"""

from __future__ import annotations

import functools
import re
import sys
from pathlib import Path

from scripts.utils.pathnorm import normalize_path

# The directory itself as well as anything inside it. A trailing separator is
# NOT required: `Grep(path="threads/personal")` is the natural spelling and  # leak-guard: ok (the guard's own vocabulary: the spelling its regex matches)
# carries none. The alternation end-anchors instead, so `threads/personal-notes/`  # leak-guard: ok (the guard's own vocabulary: the spelling its regex matches)
# is a different directory and still passes.
PERSONAL_DIR_RE = re.compile(r"threads[/\\]personal(?:[/\\]|$)", re.IGNORECASE)

# The archived copies of the same material, one directory deeper.
# `scripts/thread.py` writes a closed thread to `threads/archive/<year>/<type>/`  # leak-guard: ok (the guard's own vocabulary: the spelling its regex matches)
# and `VALID_TYPES` in `scripts/utils/threads_lib.py` includes `personal`, so the
# archive holds CEO-only bodies under a path `PERSONAL_DIR_RE` cannot see: that
# one needs `personal` immediately after `threads`.
#
# C3 (2026-10-13): the subject-tree move files the archive as
# `archive/threads/<year>/<type>/`, where `personal` follows a YEAR directly  # leak-guard: ok (the guard's own vocabulary: the spelling its regex matches)
# under `threads`. The year shape is matched here rather than any one segment,
# so `threads/business/` and its subdirectories stay ordinary business content.  # leak-guard: ok (the guard's own vocabulary: the spelling its regex matches)
PERSONAL_ARCHIVE_RE = re.compile(
    r"threads[/\\](?:archive[/\\].+|\d{4})[/\\]personal(?:[/\\]|$)", re.IGNORECASE)


@functools.lru_cache(maxsize=1)
def _registry_archive_re() -> "re.Pattern[str] | None":
    """The archive at whatever address the registry gives `threads-archive`.

    The static shapes above know two layouts; a third one would be followed by
    the registry and missed by them. Imported here, lazily, because the hook
    that calls this runs on every tool call and the resolver is not free. A
    failure is said on stderr and leaves the static shapes standing.
    """
    try:
        from scripts.utils.data_path import data_path
        from scripts.utils.paths import get_data_root
        tail = data_path("threads-archive").relative_to(get_data_root()).parts
    except Exception as exc:  # noqa: BLE001 (said, and the static shapes still hold)
        print(f"personal_paths: 'threads-archive' has no address here "
              f"({type(exc).__name__}: {exc}); only the static archive shapes apply",
              file=sys.stderr)
        return None
    sep = r"[/\\]"
    return re.compile(sep.join(re.escape(p) for p in tail) + sep + r"(?:.+" + sep + r")?"
                      + r"personal(?:[/\\]|$)", re.IGNORECASE)


#: The registry name of the personal threads' own home (HOS-235). C3 put it
#: under the personal subject root, where the two segments the static shapes
#: look for arrive in the other order, so this home is recognised by name only.
PERSONAL_HOME_NAME = "threads-personal"


@functools.lru_cache(maxsize=1)
def personal_home_dir() -> "Path | None":
    """The personal threads' home, resolved, or None when it has no address here.

    Lazy for the reason `_registry_archive_re` gives. A failure is said on
    stderr; the static shapes still guard the pre-C3 spelling.
    """
    try:
        from scripts.utils.data_path import data_path
        return data_path("threads-personal").resolve()
    except Exception as exc:  # noqa: BLE001 (said, and the static shapes still hold)
        print(f"personal_paths: {PERSONAL_HOME_NAME!r} has no address here "
              f"({type(exc).__name__}: {exc}); only the static shapes apply",
              file=sys.stderr)
        return None


@functools.lru_cache(maxsize=1)
def _home_tail() -> "tuple[str, ...]":
    """The home's segments below the data root, or () when it has none."""
    home = personal_home_dir()
    if home is None:
        return ()
    try:
        from scripts.utils.paths import get_data_root
        return home.relative_to(Path(get_data_root()).resolve()).parts
    except Exception as exc:  # noqa: BLE001 (said, and the static shapes still hold)
        print(f"personal_paths: {PERSONAL_HOME_NAME!r} is not under the data root "
              f"({type(exc).__name__}: {exc}); only the static shapes apply",
              file=sys.stderr)
        return ()


def personal_home_fragment() -> "str | None":
    """An unanchored regex fragment spelling the home, for the Bash patterns.

    Data-relative, so it matches the absolute spelling, a relative one and one
    written from inside the data root alike. The optional leading run lets a
    pattern that needs the path right after a token (`< path`, `if=path`)
    match the absolute spelling too.
    """
    tail = _home_tail()
    if not tail:
        return None
    return (r"(?:[^\s'\"]*[/\\])?" + r"[/\\]".join(re.escape(p) for p in tail)
            + r"(?![\w.-])")


@functools.lru_cache(maxsize=1)
def _home_re() -> "re.Pattern[str] | None":
    fragment = personal_home_fragment()
    return re.compile(fragment, re.IGNORECASE) if fragment else None


def _home_hint(text: str) -> bool:
    """Cheap pre-filter: the home's last segment occurs in the text at all."""
    tail = _home_tail()
    return bool(tail) and tail[-1].lower() in text.lower()


def names_personal_home(text: str) -> bool:
    """True when `text` spells the personal threads' home or a path inside it."""
    if not _home_hint(text):
        return False
    rx = _home_re()
    return bool(rx and rx.search(text))


def names_personal_archive(text: str) -> bool:
    """True when `text` spells a path inside an archived CEO-only thread.

    The static shapes first; the registry's address only for text that names a
    `personal` segment at all, so most calls never reach the resolver.
    """
    if PERSONAL_ARCHIVE_RE.search(text):
        return True
    if "personal" not in text.lower():
        return False
    rx = _registry_archive_re()
    return bool(rx and rx.search(text))

# A path-like run inside free text: anything containing a separator, stopping at
# whitespace and the shell metacharacters that end a word.
_PATH_TOKEN_RE = re.compile(r"[^\s'\"<>|;&()]*[/\\][^\s'\"<>|;&()]*")

# Cheap pre-filter. Every spelling of a CEO-only path names the threads root
# followed by a separator somewhere, so text without that cannot be made to
# match by canonicalising it, and the substitution pass is skipped.
_THREADS_HINT_RE = re.compile(r"threads[/\\]", re.IGNORECASE)


def canonicalise_paths(text: str) -> str:
    """Rewrite every path-like run in free text to its collapsed form."""
    if not (_THREADS_HINT_RE.search(text) or _home_hint(text)):
        return text
    return _PATH_TOKEN_RE.sub(lambda m: normalize_path(m.group(0)), text)


def names_personal_threads(text: str) -> bool:
    """True when `text` spells a path inside either CEO-only threads subtree.

    Asked of the raw text AND of its canonical form. The two differ exactly when
    the text carries a `.`, `//` or `..` segment, which changes the spelling and
    not the file. Until 2026-08-29 only the raw form was asked, and three
    ordinary spellings of one CEO-only file walked through the wall.
    """
    for haystack in (text, canonicalise_paths(text)):
        if (PERSONAL_DIR_RE.search(haystack) or names_personal_archive(haystack)
                or names_personal_home(haystack)):
            return True
    return False
