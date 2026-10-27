#!/usr/bin/env python3
"""A raise to any test-code ceiling must be declared in the commit message.

Runs at the `commit-msg` stage, because the thing it reads is the message and
nothing at the `pre-commit` stage can see one.

WHAT THIS ESTABLISHES, AND IT IS LESS THAN IT LOOKS. It establishes that a raise
to `config/test-budget.json` is DECLARED, in a line that lives in git history
for good and that `git log --grep` finds in one command. It does NOT establish
that the operator authorised it: an agent writes commit messages in this
repository every day and can write this line as easily as it can edit the JSON.
Saying otherwise would be exactly the defect this repository's own rules name,
a gate whose appearance exceeds what it holds. The authorisation half is held
elsewhere and by a different mechanism: the release gate in
`.claude/hooks/_dispatch.py` refuses any commit whose authorising prompt carries
the `X-HEADING-BRIEF` marker, so a commit at all requires a prompt no other
session produced.

So the guarantee is VISIBILITY over time: before this (2026-09-21) no ceiling
raise left a trace in any commit message.

NOT CLAIMED. This reads the ceiling file and the message. It says nothing about
whether the tests added are good, whether the raise was warranted, or whether
the number in the file matches the tree; `scripts/check-test-budget.py` owns
that comparison.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUDGET_REL = "config/test-budget.json"
CEILINGS = ("test_files", "test_functions", "test_code_lines")
MARKER = "Test-Budget-Raise:"


def _head_budget() -> dict | None:
    """The ceilings as HEAD holds them, or None when there is no answer.

    None means "no previous version to compare against" and is treated as
    nothing-to-refuse: the first commit that creates the file is not a raise.
    An error running git is reported rather than swallowed, because a silent
    failure here turns this gate into a no-op that still prints nothing.
    """
    try:
        out = subprocess.run(
            ["git", "show", f"HEAD:{BUDGET_REL}"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"test-budget-raise: git could not be run, so a raise cannot be "
              f"detected: {exc}", file=sys.stderr)
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except ValueError:
        return None


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: check-test-budget-raise.py <commit-msg-file>",
              file=sys.stderr)
        return 1

    before = _head_budget()
    if before is None:
        return 0

    budget_path = ROOT / BUDGET_REL
    if not budget_path.exists():
        return 0
    try:
        after = json.loads(budget_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"test-budget-raise: {BUDGET_REL} is not valid JSON: {exc}",
              file=sys.stderr)
        return 1

    raised = [
        (key, before[key], after[key])
        for key in CEILINGS
        if isinstance(before.get(key), int) and isinstance(after.get(key), int)
        and after[key] > before[key]
    ]
    if not raised:
        return 0

    message = Path(argv[0]).read_text(encoding="utf-8", errors="replace")
    # Comment lines are what git strips before the message is stored, so a
    # marker sitting in one would vanish from the history this gate exists to
    # leave a trace in.
    body = "\n".join(line for line in message.splitlines()
                     if not line.lstrip().startswith("#"))
    if MARKER in body:
        return 0

    print("test-budget-raise: REFUSED. This commit raises a ceiling on test "
          "code and the message does not say so.", file=sys.stderr)
    for key, old, new in raised:
        print(f"  {key}: {old} -> {new} (+{new - old})", file=sys.stderr)
    print(f"\nAdd one line to the commit message:\n\n"
          f"    {MARKER} <which ceiling, and the guarantee the new test "
          f"carries>\n\n"
          f"A raise is a decision and it should be readable in `git log` years "
          f"from now, which is the whole reason this is a message line rather "
          f"than a flag. If the ceiling does not need to rise, the alternative "
          f"is in .claude/rules/development-standards.md, Phase 5: the fix "
          f"ships with no test and a `Fix-Note:` line instead.",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
