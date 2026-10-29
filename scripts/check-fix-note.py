#!/usr/bin/env python3
"""A fix that ships without a test carries one line saying what it knew.

Runs at the `commit-msg` stage. Fires when a commit whose subject begins `fix:`
or `emergency:` touches `scripts/` or `.claude/hooks/` and adds no test
function. It then requires:

    Fix-Note: <what broke> | <what fixed it> | <why no test>

WHY: since 2026-09-21 a fix ships WITHOUT a regression test unless the defect
is failure A or B (`.claude/rules/development-standards.md` Phase 5), and this
line keeps the record a test used to carry. Operator decision 2026-09-21, in his
own words: **"Knowledge is not lost!"** (translated) It is greppable for good
(`git log --grep 'Fix-Note:'`).

WHAT THIS DOES NOT HOLD, SAID PLAINLY RATHER THAN IMPLIED BY SILENCE. It reads
the subject line, so a fix labelled `chore:` is not asked for a note. That is
not an oversight to be closed by widening the trigger: widening it to every
commit touching `scripts/` would demand the line on refactors, renames and
dependency bumps, where it has nothing to say, and a required field with
nothing to say in it gets filled with noise and then stops being read. It also
cannot check that the line is TRUE, or that the three parts are the three parts.
It checks that a human wrote a sentence where a test used to go.

A SECOND CHECK LIVES HERE, `--no-claude-trailer`: it refuses a commit message
carrying a `Co-Authored-By:` trailer that names Claude or `anthropic.com`
(rule M042.2; engine commit messages are public). It sits in this file rather
than in a script of its own because the test budget in `config/test-budget.json`
is at its ceiling on all three dimensions, and a new hook script would have to
be named by a test under `tests/` (`names_script` in
`scripts/check-gate-integrity.py`); this file is already a named commit-msg
hook. Its proof is the built-in `--self-test`.
"""
from __future__ import annotations

import contextlib
import io
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARKER = "Fix-Note:"
SUBJECTS = ("fix:", "emergency:")
CODE_PREFIXES = ("scripts/", ".claude/hooks/")
USAGE = ("usage: check-fix-note.py <commit-msg-file> | "
         "check-fix-note.py --no-claude-trailer <commit-msg-file> | "
         "check-fix-note.py --self-test")


def _git(*args: str) -> str | None:
    """stdout of a git command, or None when it could not be run or failed.

    BYTES, DECODED DEFENSIVELY, never `text=True`. MEASURED 2026-09-21 on the
    very commit that introduced this gate: `git diff --cached -U0 -- tests/`
    over a commit touching binary fixtures raised `UnicodeDecodeError: invalid
    start byte` inside subprocess, before this function ever saw the output.
    The hook exited 1 with a traceback and refused the commit for a reason that
    had nothing to do with what it checks. A gate that crashes on ordinary
    input is a gate somebody disables.
    """
    try:
        out = subprocess.run(["git", *args], cwd=str(ROOT),
                             capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"fix-note: git could not be run, so this gate is not checking "
              f"anything on this commit: {exc}", file=sys.stderr)
        return None
    if out.returncode != 0:
        err = (out.stderr.decode("utf-8", "replace").strip().splitlines()
               or [""])[0]
        print(f"fix-note: `git {' '.join(args)}` exited {out.returncode}, so this "
              f"gate is not checking anything on this commit: {err}",
              file=sys.stderr)
        return None
    return out.stdout.decode("utf-8", "replace")


def _staged_paths() -> list[str]:
    out = _git("diff", "--cached", "--name-only")
    return [] if out is None else [p for p in out.splitlines() if p]


def _adds_a_test_function() -> bool:
    """Whether the staged diff introduces a `def test_*` under `tests/`.

    Reads the ADDED lines of the diff rather than the files, because a commit
    that merely touches a test file already holding tests has not written a new
    one. `-U0` so no context line can be mistaken for an addition.
    """
    out = _git("diff", "--cached", "-U0", "--", "tests/")
    if not out:
        return False
    return any(
        re.match(r"\+\s*(async\s+)?def\s+test_", line)
        for line in out.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )


def claude_trailer_lines(path: str) -> list[tuple[int, str]]:
    """(1-based line number, line) of every Co-Authored-By trailer naming Claude.

    Git comment lines (first non-space character `#`) are ignored.
    """
    message = Path(path).read_text(encoding="utf-8", errors="replace")
    hits = []
    for n, line in enumerate(message.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        head, sep, rest = stripped.partition(":")
        if not sep or head.lower() != "co-authored-by":
            continue
        rest = rest.lower()
        if "claude" in rest or "anthropic.com" in rest:
            hits.append((n, line))
    return hits


def check_claude_trailer(path: str) -> int:
    hits = claude_trailer_lines(path)
    if not hits:
        return 0
    for n, line in hits:
        print(f"no-claude-coauthor: REFUSED. line {n}: {line}", file=sys.stderr)
    print("Rule M042.2: no Co-Authored-By trailer naming Claude, in any form. "
          "Remove the line and commit again.", file=sys.stderr)
    return 1


SELF_TEST_CASES = (
    ("refuse", "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>", 1),
    ("refuse", "  co-authored-by: claude <x@example.com>", 1),
    ("refuse", "Co-Authored-By: Someone <noreply@anthropic.com>", 1),
    ("pass", "Co-Authored-By: Jane Doe <jane@example.com>", 0),
    ("pass", "# Co-Authored-By: Claude <noreply@anthropic.com>", 0),
    ("pass", None, 0),
)


def self_test() -> int:
    # Imported here so the two commit-msg forms never load the data registry.
    sys.path.insert(0, str(ROOT))
    from scripts.utils.data_path import data_write_dir

    failed = 0
    with tempfile.TemporaryDirectory(
            prefix="check-fix-note-st.",
            dir=data_write_dir("runtime:scratch/tmp")) as tmp:
        for i, (kind, trailer, expected) in enumerate(SELF_TEST_CASES, 1):
            msg = Path(tmp) / f"case-{i}.msg"
            text = "feat: x\n" + (f"\n{trailer}\n" if trailer else "")
            msg.write_text(text, encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = main(["--no-claude-trailer", str(msg)])
            ok = code == expected and (expected == 0 or "M042.2" in err.getvalue())
            label = trailer if trailer else "(no trailer)"
            print(f"{'PASS' if ok else 'FAIL'} {kind}: {label} (exit {code})")
            failed += not ok
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    if argv == ["--self-test"]:
        return self_test()
    if len(argv) == 2 and argv[0] == "--no-claude-trailer":
        return check_claude_trailer(argv[1])
    if len(argv) != 1 or argv[0].startswith("--"):
        print(USAGE, file=sys.stderr)
        return 1

    message = Path(argv[0]).read_text(encoding="utf-8", errors="replace")
    body = "\n".join(line for line in message.splitlines()
                     if not line.lstrip().startswith("#"))
    subject = next((line for line in body.splitlines() if line.strip()), "")
    if not subject.lower().startswith(SUBJECTS):
        return 0

    touched = [p for p in _staged_paths() if p.startswith(CODE_PREFIXES)]
    if not touched:
        return 0

    if _adds_a_test_function():
        return 0

    if MARKER in body:
        return 0

    print("fix-note: REFUSED. This is a fix to code that ships with no test, "
          "and the message does not say what it knew.", file=sys.stderr)
    print("  touched: " + ", ".join(touched[:8])
          + (f" (+{len(touched) - 8} more)" if len(touched) > 8 else ""),
          file=sys.stderr)
    print(f"\nAdd one line:\n\n"
          f"    {MARKER} <what broke> | <what fixed it> | <why no test>\n\n"
          f"Shipping without a test is the DEFAULT here since 2026-09-21, so "
          f"this is not asking you to add one. It is asking for the sentence "
          f"the test would have carried, because the understanding is the part "
          f"that cannot be reconstructed later. If the defect belongs to "
          f"failure A (a private byte reaching this public repository) or "
          f"failure B (the two repositories merging, or a yard acting on "
          f"something live), write the test instead, as a new test FUNCTION in "
          f"the file that already holds that guarantee.",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
