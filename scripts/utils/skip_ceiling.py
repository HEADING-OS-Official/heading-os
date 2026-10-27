"""The one reader of the committed skip ceiling.

`config/nightly-skip-baseline.json` carries `max_skips`: the line between a suite
that passed and a suite that did not run. Two callers read it now, and they must
read it the same way or the night and the ordinary run disagree about what an
honest count looks like:

* `scripts/nightly-refresh.py`, which parses the count out of the gate child's
  summary text because it drives pytest as a subprocess and has nothing else;
* `scripts/utils/skip_ceiling_guard.py`, the pytest plugin armed by
  `tests/conftest.py`, which counts the reports pytest itself counts.

This module exists because the reader was a private function in
`nightly-refresh.py` until 2026-09-18, and a second copy of it in the plugin
would have been the copy nobody updated. This repository's dominant defect shape
is a fix that landed in one of N copies (`.claude/rules/development-standards.md`
obligation 3).

Tests: none live. Both halves, the night's and the plugin's, were covered until
the 2026-09-21 test-suite split. The reader and the plugin still run; what is
gone is the proof that they refuse when they should.
"""
from __future__ import annotations

import json
from pathlib import Path

#: Where the ceiling lives, relative to the engine checkout being judged.
#: Relative on purpose: the guard that reads it derives its root from the
#: current checkout, never from a constant (`CLAUDE.md`, "Guards must be armed
#: inside the task").
SKIP_BASELINE_REL = Path("config/nightly-skip-baseline.json")


def read_skip_ceiling(root: Path) -> int | None:
    """The committed maximum skip count, or None when it cannot be read.

    Absent, unparseable, missing the key, and not a non-negative integer are all
    answered None, and every caller refuses on None. A run that cannot establish
    what an honest skip count looks like cannot certify one, and the alternative
    (defaulting to some number written here) puts a second copy of the ceiling in
    the code, where it would be the one nobody updates.

    `bool` is rejected explicitly because `isinstance(True, int)` is True in
    Python and `"max_skips": true` would otherwise read as a ceiling of 1.
    """
    try:
        data = json.loads((root / SKIP_BASELINE_REL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ceiling = data.get("max_skips") if isinstance(data, dict) else None
    if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling < 0:
        return None
    return ceiling
