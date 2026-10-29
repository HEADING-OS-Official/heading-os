#!/usr/bin/env python3
"""Refuse when `.secrets.baseline` no longer matches what detect-secrets finds.

Usage:
    .venv/bin/python scripts/check-secrets-baseline-drift.py

Exit codes:
    0   the baseline's `results` equal a fresh scan's
    1   drift: the files with new, changed or gone findings are printed
    2   the scan itself could not run

One copy of the logic, run by CI (the `detect-secrets baseline drift (F-9.4)`
step of the guards job) and by `scripts/push-all.py` as a push ratchet, so the
drift is seen before a push rather than first in a CI failure mail (engine CI
was red on every push from 2026-10-29 for exactly this).

WHY IT COMPARES A COPY, AND ONLY `results`. `scan --baseline` REWRITES the
baseline in place and exits 0 whatever it finds, so a step that only ran it was
named for a drift it could not detect: a new secret updated the file and the job
stayed green.

`git diff --exit-code` is NOT the fix and was tried first. MEASURED 2026-09-05:
two consecutive scans of an unchanged tree differ, because `generated_at` is
stamped on every run. That gate would have failed every build forever, which is
a gate nobody keeps.

So the comparison is over `results` alone, and the scan writes to a COPY: a step
that rewrites a tracked file leaves the checkout dirty for everything after it.
`results` is the right key to compare because the baseline's self-reference
lives in `filters_used`, which changes with the copy's path and would otherwise
differ on every run.

Both halves MEASURED 2026-09-05 against the refreshed baseline: an unchanged
tree passes, and a 64-hex literal planted in a new file fails with that file
named. The planted file had to be `git add -N`'d first, because detect-secrets
walks the git index and an UNTRACKED file is invisible to it. That costs nothing
in CI (a checkout tracks everything) but it is why this gate is not the one that
catches a secret in a scratch file; the push wall's content scan is.

The copy goes to `$RUNNER_TEMP` in CI, otherwise to this checkout's scratch
(`scripts/data-path.py scratch`), never `/tmp`.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / ".secrets.baseline"


def work_dir() -> Path:
    runner_temp = os.environ.get("RUNNER_TEMP")
    if runner_temp:
        return Path(runner_temp)
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "data-path.py"), "scratch"],
                          capture_output=True, text=True, check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(f"no scratch directory: {proc.stderr.strip()}")
    return Path(proc.stdout.strip().splitlines()[-1])


def main() -> int:
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args()
    try:
        work = work_dir()
        work.mkdir(parents=True, exist_ok=True)
        copy = work / "baseline.new"
        shutil.copyfile(BASELINE, copy)
        proc = subprocess.run([sys.executable, "-m", "detect_secrets", "scan", "--baseline", str(copy)],
                              cwd=str(ROOT), capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"detect-secrets scan exited {proc.returncode}: {proc.stderr.strip()[-400:]}")
        old = json.loads(BASELINE.read_text(encoding="utf-8"))["results"]
        new = json.loads(copy.read_text(encoding="utf-8"))["results"]
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print(f"check-secrets-baseline-drift: cannot run: {exc}", file=sys.stderr)
        return 2
    if old == new:
        print("detect-secrets baseline: no drift")
        return 0
    added = {f: new[f] for f in new if f not in old or new[f] != old[f]}
    gone = [f for f in old if f not in new]
    print("detect-secrets baseline is out of date.")
    for f in sorted(added):
        print(f"  new or changed findings in {f}")
    for f in sorted(gone):
        print(f"  findings gone from {f}")
    print("Review each, then refresh with:")
    print("  detect-secrets scan --baseline .secrets.baseline")
    return 1


if __name__ == "__main__":
    sys.exit(main())
