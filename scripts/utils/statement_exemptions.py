"""Reviewed exemptions keyed by the statement they cover, one format for every gate.

Two gates read a file of this shape: `scripts/dev/import-env-purity.py` over
`config/import-env-purity-exemptions.json` (HOS-116), and the HOS-5 section 6
prohibition in `scripts/check-data-path-references.py` over
`config/data-path-pins-exemptions.json`. The format and its validation live
here once, because two copies of "what makes an entry valid" are two gates free
to disagree about the same file shape. What counts as a FINDING stays with each
gate; only the entry, its key and the validation are shared.

An entry is `{"path", "statement", "reason"}`. It is keyed by the file plus the
sha256 of the statement's text, every line of it, whitespace collapsed. The
entry STORES the statement and the key is computed from it, so what a reviewer
reads is exactly what is keyed. A changed statement is a new finding and leaves
its old entry stale, and both gates refuse a stale entry, so a review cannot
outlive the code it reviewed. A reason shorter than `REASON_MIN_WORDS` words is
refused, because a fragment reads as an explanation and says nothing.

The file is valid as a whole or not at all: a skipped malformed entry would
leave its finding uncovered, and the refusal would then call it new code when
the file is what is broken.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import NamedTuple

#: Fewer words than this is a fragment, not a reason.
REASON_MIN_WORDS = 8


def line_key(text: str) -> str:
    """The sha256 an entry is keyed by: the statement, whitespace collapsed."""
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()


class Entry(NamedTuple):
    path: str       # relative to the engine root, POSIX separators
    statement: str  # the covered statement's text, every line of it
    reason: str

    @property
    def key(self) -> str:
        return line_key(self.statement)


class ExemptionsError(Exception):
    """The exemptions file cannot be read as the reviewed list it claims to be."""


def load_exemptions(path: Path) -> list[Entry]:
    """Read an exemptions file; see `entries_from` for what makes it valid."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExemptionsError(f"{path}: {type(exc).__name__}: {exc}") from exc
    return entries_from(data, str(path))


def entries_from(data: object, where: str) -> list[Entry]:
    """Validate the entries. Any defect raises, and no entry is skipped."""
    raw = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise ExemptionsError(f"{where}: no `entries` list")
    out: list[Entry] = []
    for i, item in enumerate(raw):
        at = f"{where} entry {i}"
        if not isinstance(item, dict):
            raise ExemptionsError(f"{at}: not an object")
        fields = {k: item.get(k) for k in Entry._fields}
        missing = [k for k, v in fields.items() if not isinstance(v, str) or not v]
        if missing:
            raise ExemptionsError(f"{at}: missing or empty {', '.join(missing)}")
        entry = Entry(**fields)
        rel = PurePosixPath(entry.path)
        if (rel.is_absolute() or ".." in rel.parts or "\\" in entry.path
                or rel.as_posix() != entry.path):
            raise ExemptionsError(
                f"{at}: path {entry.path!r} is not a plain path relative to "
                f"the engine root, so no finding could ever match it")
        if len(entry.reason.split()) < REASON_MIN_WORDS:
            raise ExemptionsError(
                f"{at}: the reason is a fragment; write at least "
                f"{REASON_MIN_WORDS} words that say why the exemption is intended")
        out.append(entry)
    return out


def match(keys: Iterable[tuple[str, str]],
          entries: list[Entry]) -> tuple[list[tuple[str, str]], list[Entry]]:
    """(uncovered finding keys, stale entries) for `(path, line_key)` findings.

    Counted, not set-matched: two identical statements in one file are two
    findings and need two entries, so a copy of a reviewed statement cannot take
    cover under the review of the first.
    """
    pending: dict[tuple[str, str], int] = {}
    for key in keys:
        pending[key] = pending.get(key, 0) + 1
    stale: list[Entry] = []
    for entry in entries:
        k = (entry.path, entry.key)
        if pending.get(k, 0) > 0:
            pending[k] -= 1
        else:
            stale.append(entry)
    uncovered = [k for k, n in pending.items() for _ in range(n)]
    return uncovered, stale
