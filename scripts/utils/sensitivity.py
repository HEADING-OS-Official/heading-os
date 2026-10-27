#!/usr/bin/env python3
"""Record-level `sensitivity:` and the one question the outbound send paths ask it.

WHY THIS EXISTS. The memory index reads every record, ceo-only included
(operator directive 2026-09-12), so the boundary sits where a file leaves this
machine: an attachment on an outbound send. `scripts/send-email.py`,
`scripts/gmail-draft.py` and the Telegram skill's client ask `publish_refusal()`
for every file they attach and refuse one marked `ceo-only`. Nothing else in
those paths reads a record-level field.

WHY `sensitivity:` AND NOT `classification:`. `classification:` is a required,
free-text field of the thread schema (`scripts/utils/threads_lib.py`) with no
enum, so giving it a refusing meaning would let one legacy spelling block a send
channel. `sensitivity:` is new, so its value space is defined here:
`open | internal | ceo-only`. `classification: ceo-only` is honoured as a legacy
alias for the refusal, and ONLY for the refusal.

THE FAIL-CLOSED DIRECTION, stated precisely. Every value this module cannot
prove sendable is refused: an unrecognised spelling, a non-scalar, a null,
frontmatter that will not parse, a file that will not open. A record that makes
no claim at all is NOT refused, by decision: most attachments are PDFs, fonts,
spreadsheets and images that can never carry frontmatter (measured 2026-09-12),
so refusing on absence would refuse them permanently. The record-level layer
only SUBTRACTS; it never permits what the caller would otherwise refuse.

A file whose bytes are not UTF-8 is treated as "not a record" and passes,
because that is what a font and a PDF are. A ceo-only record stored in another
encoding would pass too; on a single-operator machine the threat defended
against is a marked record leaving because nothing looked, not an adversary.

Usage:
    from scripts.utils.sensitivity import publish_refusal
    reason = publish_refusal(path)
    if reason:
        ...   # do not copy this file; print `reason`

    python -m scripts.utils.sensitivity <path> [<path> ...]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.utils.markdown import (  # noqa: E402
    FM_EMPTY,
    FM_NO_CLOSING,
    FM_NO_OPENING,
    FM_OK,
    parse_frontmatter_strict,
)

SENSITIVITY_KEY = "sensitivity"
LEGACY_KEY = "classification"

OPEN = "open"
INTERNAL = "internal"
CEO_ONLY = "ceo-only"

#: The values that permit a record to leave this machine as an attachment.
PUBLISHABLE = frozenset({OPEN, INTERNAL})
#: Every value this module recognises at all. Anything else is refused.
KNOWN = frozenset({OPEN, INTERNAL, CEO_ONLY})

# `FM_EMPTY` is `---\n---`: a fence pair around nothing. That is a document with
# no keys, which is a document making no claim, not a document this module
# failed to read. Every OTHER non-OK kind means either "there was no frontmatter
# here" (a leading `---` that is a horizontal rule) or "there was, and it did not
# parse". The first group is also no-claim. Only the parse failures refuse, and
# they are listed by exclusion so a future kind added to markdown.py arrives in
# the refusing set rather than silently in the permissive one.
_NO_CLAIM_KINDS = frozenset({FM_OK, FM_EMPTY})
_NO_FRONTMATTER_KINDS = frozenset({FM_NO_OPENING, FM_NO_CLOSING})


def normalise(value: object) -> str | None:
    """A frontmatter value as a comparable token, or None if it is not scalar.

    Case-folded and stripped, so `CEO-Only` (the spelling the operator actually
    writes) and `ceo-only` are one value. A list or a mapping returns None and
    the caller refuses: a sensitivity that is a list is not a sensitivity.
    """
    if value is None or isinstance(value, (list, dict, tuple, set)):
        return None
    return str(value).strip().casefold()


def declared_sensitivity(text: str) -> tuple[str | None, str | None, str | None]:
    """`(key, value, undecidable)` for one document's text.

    `value` is the normalised `sensitivity:` token when the document declares
    one, else `ceo-only` when it carries the legacy `classification: ceo-only`,
    else None for "this record makes no claim". `key` names which of the two
    keys the value came from, so a refusal can quote the line the operator
    actually wrote rather than the one this module prefers.

    `undecidable` is set instead when the document HAS frontmatter that could
    not be read: the caller must refuse, because a record that might say
    `ceo-only` and cannot be parsed is not a record that said nothing.
    """
    data, kind, detail = parse_frontmatter_strict(text)
    if kind not in _NO_CLAIM_KINDS:
        if kind in _NO_FRONTMATTER_KINDS:
            return None, None, None
        # First line of the PyYAML message only. The rest is a source excerpt
        # with a caret, which is three lines of noise inside a one-line refusal
        # printed beside many other files.
        first = detail.splitlines()[0].strip() if detail else ""
        return None, None, (f"frontmatter did not parse ({kind}"
                            f"{': ' + first if first else ''})")
    if not data:
        return None, None, None

    if SENSITIVITY_KEY in data:
        token = normalise(data[SENSITIVITY_KEY])
        if token is None:
            return SENSITIVITY_KEY, None, (
                f"`{SENSITIVITY_KEY}:` is not a single value "
                f"({type(data[SENSITIVITY_KEY]).__name__})")
        return SENSITIVITY_KEY, token, None

    if normalise(data.get(LEGACY_KEY)) == CEO_ONLY:
        return LEGACY_KEY, CEO_ONLY, None
    return None, None, None


def publish_refusal(path: Path) -> str | None:
    """Why this file must not leave this machine as an attachment, or None.

    None means "this record does not forbid it", NOT "send this": the caller
    still decides what it sends. See the module docstring for why absence
    permits.
    """
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        return f"could not be read, so its sensitivity is unknown: {exc}"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # Not text, so not a record with frontmatter. Fonts, PDFs, spreadsheets
        # and images all land here and all must continue to be sendable.
        return None

    key, value, undecidable = declared_sensitivity(text)
    if undecidable:
        return undecidable
    if value is None:
        return None
    if value in PUBLISHABLE:
        return None
    if value == CEO_ONLY:
        return f"marked `{key}: {CEO_ONLY}`"
    return (f"`{key}: {value}` is not one of "
            f"{sorted(KNOWN)}, so it cannot be read as sendable")


def _main(argv: list[str]) -> int:
    """Ask the guard about named files from a terminal, with no send.

    Exit 1 when any named file would be refused, so it composes into a shell
    check. This is the console path for a question that would otherwise only be
    answerable by attempting a send.
    """
    if not argv:
        print("usage: python -m scripts.utils.sensitivity <path> [<path> ...]",
              file=sys.stderr)
        return 2
    refused = 0
    for arg in argv:
        reason = publish_refusal(Path(arg))
        if reason:
            refused += 1
            print(f"REFUSE  {arg}: {reason}")
        else:
            print(f"ALLOW   {arg}")
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
