#!/usr/bin/env python3
"""Where the recall-threshold calibration records live, on both sides of the move.

One directory, two programs, and they must never disagree about it.
`scripts/recall-calibration.py` WRITES `current.json`, `pending.json`,
`trend.jsonl` and the dated `*_threshold-grid.json` here, on a weekly timer.
`scripts/memory-index.py` READS `current.json` from here on EVERY invocation, in
its config load, and treats an unreadable one as "no calibration" and falls back
to the shipped cut.

That pairing is why this is a module rather than the same two lines written out
twice. `memory-index.py` is kebab-case and cannot be imported, so the two halves
have no way to share a derivation except through a third file, and this
repository's dominant defect is a fix that landed in one of N copies. The
consequence here is specific and silent: a writer and a reader that resolve
differently do not error, they simply stop meeting, and the READER's side of
that failure is a `return None` that looks exactly like "no calibration has been
recorded yet".

The address is the registry name `memory-calibration`, spelled in the one
function below; both programs resolve through it (off the migration seam since
2026-09-24, HOS-5).
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
if str(_HERE.parents[2]) not in sys.path:
    sys.path.insert(0, str(_HERE.parents[2]))

from scripts.utils.data_path import data_path  # noqa: E402

#: No constant holds the registry name: `scripts/leak-guard.py` refuses one
#: (2026-09-24), because a name behind a constant is one no gate can attribute.

#: The calibrated cut `memory-index` reads. Written by `recall-calibration check`
#: only when a measured grid beats the shipped value inside the envelope.
CURRENT_NAME = "current.json"

#: The proposal `recall-calibration check` leaves when it changes nothing.
PENDING_NAME = "pending.json"


def calibration_dir() -> Path:
    """The directory holding the calibration records: `memory-calibration`.

    A tree whose registry cannot answer raises `DataPathError`. The reader
    (`memory-index.calibrated_threshold()`) catches it and falls back to the
    shipped cut; the writer (`scripts/recall-calibration.py`) lets it raise,
    because refusing beats creating a directory at an address nobody
    established. There is no `data_root` parameter (removed by HOS-75): isolate
    this store through `HEADING_OS_DATA`, which every reader here honours.
    """
    return data_path("memory-calibration")


def current_path() -> Path:
    """The calibrated-threshold file, whether or not it exists.

    No `data_root` parameter, for the reason `calibration_dir` gives: it had one
    and could not honour it.
    """
    return calibration_dir() / CURRENT_NAME
