#!/usr/bin/env python3
"""Run, on this checkout, exactly the engine ratchets `scripts/push-all.py` runs.

Usage:
    python scripts/check-push-ratchets.py             # verdict lines on a pass
    python scripts/check-push-ratchets.py --verbose   # every scanner's full report

Exit codes:
    0   no ratchet refused. A ratchet whose scanner is absent is skipped, as
        push-all skips it, and the skip is printed.
    1   at least one ratchet refused; push-all would refuse the engine push.

WHY THIS EXISTS (HOS-125). No ratchet except engine-only runs at pre-commit,
by operator decision 2026-09-22, so a finding stayed invisible until the push. MEASURED
2026-09-25: a yard lowered the legacy-outputs count from 30 to 29, ran that
scanner WITHOUT `--check` (exit 0 whatever it finds), reported it passing, and
push-all refused the engine push after the merge. This command runs the argv
push-all runs, read from the one table in `scripts/utils/push_ratchets.py`, so
the two cannot ask different questions.

WHO RUNS IT, AND WHEN. A yard, before it reports done, and it states the exit
code in its closing comment. HELM, on the MERGED tree before every merge commit:
`git merge --no-commit <branch>`, this command, then the commit. Two branches
that each pass alone can refuse together, which is why the merged tree is the
one that counts.

WHAT IT DOES NOT DO. It runs nothing else push-all runs: the secret, routing and
content walls read the unpushed range and belong to the push. It commits,
pushes and writes nothing, and it works in a yard, because nothing in it acts
on anything live.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.venv_guard import ensure_venv  # noqa: E402

ensure_venv()

from scripts.utils.colors import BOLD, GRAY, GREEN, RED, RESET, YELLOW  # noqa: E402
from scripts.utils.push_ratchets import RATCHETS, run_ratchet, scanner_path  # noqa: E402

#: The checkout this file sits in. Derived from the file rather than from the
#: environment, so a yard checks the yard's tree and never the main clone's.
REPO = Path(__file__).resolve().parent.parent

#: Lines of a PASSING scanner's output to print without --verbose.
PASS_TAIL = 2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--verbose", action="store_true",
                    help="print each scanner's full report even when it passes")
    args = ap.parse_args()

    refused, skipped = [], []
    for ratchet in RATCHETS:
        argv = " ".join((ratchet.script, *ratchet.args))
        print(f"{BOLD}== {ratchet.name}: {argv}{RESET}")
        proc = run_ratchet(REPO, ratchet)
        if proc is None:
            skipped.append(ratchet.name)
            print(f"{YELLOW}SKIPPED: {scanner_path(REPO, ratchet)} is absent, "
                  f"and push-all skips it too.{RESET}\n")
            continue
        # A refusal prints everything. A pass prints the scanner's closing
        # lines, which is where both put their verdict: this command printed
        # 1176 lines on a pass on 2026-09-25, and a yard reading that on every
        # run reads none of it.
        lines = (proc.stdout + proc.stderr).rstrip().splitlines()
        if proc.returncode == 0 and not args.verbose:
            lines = [ln for ln in lines if ln.strip()][-PASS_TAIL:]
        if lines:
            print("\n".join(lines))
        if proc.returncode == 0:
            print(f"{GREEN}exit 0: passed{RESET}\n")
        else:
            refused.append(ratchet.name)
            print(f"{RED}exit {proc.returncode}: REFUSED, push-all would refuse "
                  f"the engine push{RESET}\n")

    ran = len(RATCHETS) - len(skipped)
    summary = f"push ratchets on {REPO}: {ran} run, {len(refused)} refused"
    if skipped:
        summary += f", {len(skipped)} skipped ({', '.join(skipped)})"
    if refused:
        print(f"{RED}{BOLD}{summary}: {', '.join(refused)}{RESET}")
        return 1
    print(f"{GREEN}{summary}{RESET}" if not skipped else f"{GRAY}{summary}{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
