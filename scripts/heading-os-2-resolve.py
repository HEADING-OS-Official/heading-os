#!/usr/bin/env python3
"""Console entry point for the HEADING OS 2 family seam.

Usage:
    python scripts/heading-os-2-resolve.py write work deliverable
    python scripts/heading-os-2-resolve.py read trace scratch
    python scripts/heading-os-2-resolve.py read canon principle --files
    python scripts/heading-os-2-resolve.py where
    python scripts/heading-os-2-resolve.py list
    python scripts/heading-os-2-resolve.py check

Console-first: every question the seam can answer is a subcommand here, nothing
needs a browser, and no unit or timer is installed by any of them. Read-only --
it never creates a directory and never touches a record.

Exit codes: 0 answered, 1 the manifest is incomplete, 2 refused (and the refusal
says why on stderr).

The logic is in `scripts/heading_os_2/resolve.py`; this file exists so the
subcommands can be run without `-m` and without a PYTHONPATH.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# HOS-240 (2026-10-13): kept, class (d): the migration resolver's own console entry point, for inspecting the family manifest
from scripts.heading_os_2.resolve import _cli  # noqa: E402

if __name__ == "__main__":
    sys.exit(_cli())
