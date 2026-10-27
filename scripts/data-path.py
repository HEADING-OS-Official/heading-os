#!/usr/bin/env python3
"""Resolve a logical data-address name to a path, from a shell.

    python scripts/data-path.py path operations-reports
    python scripts/data-path.py path runtime:browser/cache
    python scripts/data-path.py path crm-contact person=jane
    python scripts/data-path.py read --required crm-config
    python scripts/data-path.py scratch       # this checkout's scratch, created
    python scripts/data-path.py names
    python scripts/data-path.py accounts                 # every account slug
    python scripts/data-path.py each account-threads     # slug<TAB>address, per account
    python scripts/data-path.py where
    SCRATCH="$(python scripts/data-path.py path --create runtime:scratch/tmp)"
    python scripts/data-path.py synthetic "$SCRATCH/a-throwaway-overlay"

This exists because a shell cannot call a Python function. Without it every
skill, agent brief, rule and shell script stays on literal addresses, and
`scripts/audit-skill-bash-paths.py` has nothing to point them at: the prose
half of HOS-5 is unreachable through the Python seam alone.

It resolves nothing itself. The logic is `scripts/utils/data_path.py`, which
dispatches into the two resolvers that already answer -- records through
`scripts/heading_os_2/resolve.py`, runtime state through
`scripts/utils/runtime_dir.py` -- so the terminal and the engine's own code get
the same answer from the same place rather than from two.

Exit 0 on an answer, 2 on a refusal, with the reason on stderr. It creates no
directory and writes nothing beyond the resolver's own parsed-registry cache
(`runtime:data-path/cache`), except `path --create`, which creates the address
it prints, `synthetic`, which builds a throwaway data repository at the path you
name, and `scratch`, which creates this checkout's scratch directory in the data
repository (the registry name `checkout-scratch`) and prints it. That directory replaced `<checkout>/.tmp/` on 2026-09-25 (HOS-5
section 7): a prepared commit message, a registry stanza for HELM and a raw
measurement go there, never into the engine checkout.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.data_path import _cli  # noqa: E402

if __name__ == "__main__":
    sys.exit(_cli())
