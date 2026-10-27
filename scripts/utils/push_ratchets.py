"""The engine ratchets `scripts/push-all.py` runs before a push, defined once.

Two consumers read this table and nothing else: `data_path_ratchet`,
`legacy_outputs_ratchet`, `import_env_purity_ratchet` and
`outside_workspace_writes_ratchet`, `engine_dir_writes_ratchet`,
`engine_only_ratchet`, `skill_data_paths_ratchet`,
`engine_data_paths_ratchet` and `runtime_write_guard_ratchet` in
`scripts/push-all.py`, which refuse the push, and
`scripts/check-push-ratchets.py`, which runs the same argv on any checkout,
a yard included, so the refusal can be seen before a merge rather than at the
push. Until HOS-125 each push-all function carried its own literal argv, so a
second command would have been a second copy free to drift from the first.

Why a separate command was needed at all. No ratchet runs at pre-commit, by
operator decision 2026-09-22 for the first two and 2026-09-25 for the third;
the fourth and fifth, added 2026-09-25 under HOS-5 section 7 (defects 5 and
4), follow the third's reason (the reasons are in the push-all docstrings), so a finding stayed invisible
until the push. MEASURED 2026-09-25:
a yard lowered the legacy-outputs count from 30 to 29, ran the scanner without
`--check`, which exits 0 whatever it finds, reported it passing, and push-all
refused the engine push after the merge. The argv here carries `--check`, so
the command and the push ask the same question in the same words.

A ratchet whose scanner is absent is skipped by both consumers, and both say
so. Nothing here writes: no argv carries a `--write-*` flag.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import NamedTuple


class Ratchet(NamedTuple):
    name: str
    script: str
    args: tuple[str, ...]


DATA_PATH = Ratchet(
    "data-path",
    "scripts/check-data-path-references.py",
    ("--ratchet", "--pins-check", "--skip-without-overlay"),
)
LEGACY_OUTPUTS = Ratchet(
    "legacy-outputs",
    "scripts/check-legacy-outputs-sites.py",
    ("--check",),
)

#: Refuses any import-time environment write that neither its one rule nor a
#: reviewed entry in `config/import-env-purity-exemptions.json` covers, and any
#: entry that matches nothing (HOS-116). No argv: the default run is the gate.
IMPORT_ENV_PURITY = Ratchet(
    "import-env-purity",
    "scripts/dev/import-env-purity.py",
    (),
)

#: Refuses engine code that writes a Workspace artifact outside the Workspace
#: (a `tempfile` call with no directory, `gettempdir()`, a `/tmp` literal)
#: unless a reviewed entry in
#: `config/outside-workspace-writes-exemptions.json` covers it, and any entry
#: that matches nothing (HOS-5 section 7, defect 5). No argv: the default run
#: is the gate.
OUTSIDE_WORKSPACE_WRITES = Ratchet(
    "outside-workspace-writes",
    "scripts/dev/outside-workspace-writes.py",
    (),
)

#: Refuses any address the engine spells INSIDE its own checkout that the
#: checkout's `.gitignore` ignores, the hiding place every other wall is blind
#: to, unless a reviewed entry in `config/engine-dir-writes-exemptions.json`
#: covers it; an entry matching nothing refuses too (HOS-5 section 7, defect 4).
#: No argv.
ENGINE_DIR_WRITES = Ratchet(
    "engine-dir-writes",
    "scripts/dev/engine-dir-writes.py",
    (),
)

#: Refuses any tracked engine file that `config/engine-allowlist.yaml` does not
#: cover and the shrink-only `config/engine-only-baseline.json` does not list,
#: and any baseline entry that went stale or was added (HOS-131, 2026-09-25).
#: The only ratchet here that ALSO runs at pre-commit; the checker's docstring
#: says why. `--carried` adds untracked-not-ignored files, which push-all's
#: own `git add -A` would commit after this runs.
ENGINE_ONLY = Ratchet(
    "engine-only",
    "scripts/check-engine-only.py",
    ("--carried",),
)

#: Refuses a skill that names a data-repository address by its physical path
#: (`status/entity/crm/contacts/`) instead of a logical name resolved through  # leak-guard: ok (an example of the physical spelling the gate refuses)
#: `scripts/data-path.py`, beyond the shrink-only ceiling in
#: `config/skill-data-paths-exemptions.json`, and any stale reviewed entry
#: there (2026-10-12, before the data repository's folder move).
SKILL_DATA_PATHS = Ratchet(
    "skill-data-paths",
    "scripts/audit-skill-data-paths.py",
    ("--check",),
)

#: The same question over engine code: scripts, hooks, agents and engine
#: config, against `config/engine-data-paths-exemptions.json`, whose ceiling
#: may only fall and ends at 0 (HOS-234, 2026-10-13).
ENGINE_DATA_PATHS = Ratchet(
    "engine-data-paths",
    "scripts/audit-skill-data-paths.py",
    ("--engine", "--check"),
)

#: Refuses an overlay write that bypasses the front door, and any
#: `RECORD_EXEMPTIONS` row that forgives no site (engine audit q32, 2026-10-16).
RUNTIME_WRITE_GUARD = Ratchet(
    "runtime-write-guard",
    "scripts/utils/runtime_write_guard.py",
    (),
)

#: The same question over the DATA repository's own config files, against the
#: ledger at the registry name `data-config-exemptions`, ceiling 0 (HOS-234 part
#: 5, 2026-10-13). push-all runs it before the DATA push, from the engine clone,
#: because the data repository is pushed first and carries no scanner of its own.
DATA_CONFIG_PATHS = Ratchet(
    "data-config-paths",
    "scripts/audit-skill-data-paths.py",
    ("--data-config", "--check"),
)

#: In the order push-all runs them.
RATCHETS: tuple[Ratchet, ...] = (DATA_PATH, LEGACY_OUTPUTS, IMPORT_ENV_PURITY,
                                 OUTSIDE_WORKSPACE_WRITES, ENGINE_DIR_WRITES,
                                 ENGINE_ONLY, SKILL_DATA_PATHS, ENGINE_DATA_PATHS,
                                 RUNTIME_WRITE_GUARD, DATA_CONFIG_PATHS)


def scanner_path(repo: Path, ratchet: Ratchet) -> Path:
    return repo / ratchet.script


def run_ratchet(repo: Path, ratchet: Ratchet) -> subprocess.CompletedProcess | None:
    """Run one ratchet on `repo`; None when its scanner is absent there."""
    scanner = scanner_path(repo, ratchet)
    if not scanner.is_file():
        return None
    return subprocess.run(  # nosec B603 - fixed argv, no shell
        [sys.executable, str(scanner), *ratchet.args],
        cwd=str(repo), capture_output=True, text=True, check=False,
    )
