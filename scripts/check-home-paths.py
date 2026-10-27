#!/usr/bin/env python3
"""Guard: a real user's home directory, spelled out in a tracked engine file.

Usage:
    python scripts/check-home-paths.py              # report
    python scripts/check-home-paths.py --check      # exit 1 on any finding
    python scripts/check-home-paths.py --json

What it refuses, and why that is worth a gate
---------------------------------------------
This repository is PUBLIC. An absolute path rooted in somebody's home directory
publishes three things at once: the unix (or Windows) account name, the layout
of that machine, and the names of the working trees on it. `.claude/rules` and
the operator's M011.1 forbid it in so many words, and nothing in this tree
enforced it until 2026-09-21. `scripts/leak-guard.py` and the `content-guard`
hook look for private CONTENT and for data-overlay ADDRESSES; a machine path is
neither, so both walked past it.

On the day it landed (2026-09-21) this guard found more sites than a `git grep`
for one account name did, because some were spelled with the macOS root.

It refuses the SHAPE, not one account name
-------------------------------------------
A guard that greps `administrator` is satisfied the moment the work is done on
a second machine by a second person, and satisfied silently. So the default
here is REFUSAL of every home-rooted absolute path, and a path gets through only
by naming an account this file declares invented, or by carrying a visible
exemption on its own line.

    /home/<account>              POSIX
    /Users/<account>             macOS
    <drive>:\\Users\\<account>   Windows

**PLACEHOLDER_ACCOUNTS is not an escape hatch, and widening it to pass your own
commit is the move `.claude/rules` forbids outright** (the operator's M042.1:
never add a vocabulary word to a gate in order to pass your own commit). It
exists so that the invented names ALREADY carrying this tree's examples keep
working. Writing a new example? Use a name that is already on the list. Needing
a real path in a tracked file? Use the marker below, where a reviewer can see it.

The way through, and it is visible from the file itself
--------------------------------------------------------
    home-path: ok (<reason, at least MIN_REASON_CHARS characters>)

on the finding's own line or the line directly above it. The spelling is
`scripts/leak-guard.py`'s `# leak-guard: ok (<reason>)` with the guard's name
swapped, because this tree already has that idiom and a second shape for the
same job is a second thing to remember. The `#` is NOT required here: most of
what this guard reads is Markdown, a docstring or a JSON value, and a comment
character cannot be appended to any of those.

A baseline file was considered and deliberately NOT built, which is the one
place this departs from its two siblings, `scripts/check-live-data-root.py` and
`scripts/check-test-vacuity.py`. Their grandfathered sites live in
`config/*-baseline.json`, and that is right for a ratchet over debt nobody is
clearing today. Here the count reaches zero in the change that adds the guard,
so a baseline would hold nothing; and an entry in one is an exemption a reader
of the offending FILE cannot see, which is the failure mode this workspace has
already paid for twice.

Scope, precisely (`.claude/rules/scope-claims.md`)
--------------------------------------------------
It reads git's INDEX (`git ls-files`), so a file staged but not committed is
included, which is the property a pre-commit gate needs. Every path that decodes
as UTF-8 and holds no NUL byte is scanned as TEXT; the rest are counted and
named in the report rather than skipped in silence.

A TEXT scan, not an AST one, and the difference from the craft rule is the
SUBJECT. That rule ("ask the AST, not the text") is about verifying a control by
grepping source for a pattern, where a comment explaining the pattern goes red.
Here the literal text IS what leaks: the same string is a defect in a docstring,
in a Markdown code fence, in a JSON value and in a comment, and most of the files
this reads have no AST at all. The cost is real and is stated rather than
implied: this file's own prose carries the shapes it refuses, and it passes only
because the account names in them are declared placeholders.

WHAT IT CANNOT SEE, said rather than left to be discovered:

* `/root` is not checked. It is the same path on every machine and names no
  account, so it publishes nothing about whose machine it is.
* A home path assembled at run time from two pieces (`"/home/" + user`) is
  invisible, as is one that arrives from the environment. This reads literals.
* An account name outside `_SEGMENT`'s character class does not match at all.
  That class covers a POSIX account name (letters, digits, `_`, `-`, `.`) and
  the punctuation placeholders are written with; a name outside it is missed.
* The marker is a PRESENCE-AND-LENGTH test, exactly like the reason floor in
  `scripts/check-live-data-root.py`. It cannot tell a real reason from forty
  characters of filler, and whether an exemption is warranted is the reviewer's
  call. The floor exists so the reviewer has something to read at all.
* Nothing here says anything about the repository's HISTORY, which is published
  with it. Rewriting that is a separate, manual decision of the operator's,
  recorded as a condition in `CLAUDE.md` before the engine is ever made public.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.colors import BOLD, GRAY, GREEN, RED, RESET, YELLOW  # noqa: E402
from scripts.utils.repo_files import IndexUnreadable, git_index_paths  # noqa: E402
from scripts.utils.workspace import get_workspace_root  # noqa: E402

ROOT = get_workspace_root()

#: The inline exemption, same shape as `scripts/leak-guard.py`'s marker.
MARKER = "home-path: ok"

#: The floor on an exemption's reason, borrowed from
#: `scripts/check-live-data-root.py` along with everything else about the
#: adjacent-comment rule, so the two behave alike.
MIN_REASON_CHARS = 40

#: Below these, the walk itself has gone wrong and a clean verdict would be a lie
#: about coverage. The floor is a SHARE of the paths git lists rather than a
#: count, so the engine shrinking does not trip it; the absolute minimum only
#: says the listing is this repository at all. An absolute floor of 1000 stood
#: here until 2026-10-16, about 90 files under the tree it guarded.
MIN_TEXT_SHARE = 0.75
MIN_CORPUS = 100

#: Account names this tree has already declared invented, in its examples, its
#: documented shell commands and its fixtures. Case-folded on comparison.
#:
#: READ THE PARAGRAPH IN THE MODULE DOCSTRING BEFORE ADDING ONE. Adding a name
#: here to get your own commit past this gate is forbidden; the sanctioned way
#: through is the inline marker, which a reviewer can see from the file that
#: carries the path.
PLACEHOLDER_ACCOUNTS = frozenset({
    "alice", "bob", "builder", "example", "hosync", "me", "name", "op",
    "operator", "real", "some", "someone", "svc", "user", "users", "x", "you",
    "youruser",
})

#: A segment whose SPELLING is a placeholder, whatever word is inside it:
#: `<user>`, `${USER}`, `{slug}`, `~`, a glob `*`, a regex fragment `[^/]+`, a
#: `%USERNAME%`. These need no vocabulary entry, and that is the point: a
#: genuinely generic example is recognisable from its own punctuation.
_PLACEHOLDER_SHAPE = re.compile(r"[<>{}$~*\[\]^%]")

#: An elision in prose (`/home/.../x`) names no account either.
_ELLIPSIS = re.compile(r"^\.{2,}$")

#: The characters an account segment may be spelled with. Deliberately explicit
#: rather than a negated class: see the scope section for what it therefore
#: misses.
_SEGMENT = r"[A-Za-z0-9_.$~<>{}\[\]^%*+@-]+"

#: The three home roots. `\\+` because a Windows path is written with one
#: backslash in Markdown and two in a Python literal, and both spellings are in
#: this tree. Case-insensitive, because `/users/` appears lower-cased inside
#: regex fragments.
HOME_RE = re.compile(
    r"(?:/home/|/users/|[A-Za-z]:\\+users\\+)(" + _SEGMENT + r")",
    re.IGNORECASE,
)

#: Suffixes whose bytes are not prose. Read as a fast path only: a file that
#: decodes as UTF-8 and holds no NUL is scanned whatever it is called, so a
#: mislabelled file is not silently dropped.
BINARY_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".ico", ".gif", ".pdf", ".zip",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".pptx", ".docx", ".xlsx",
    ".db", ".sqlite", ".so", ".pyc", ".mp4", ".mp3", ".wav",
})


# ============================================================
# The rule, pure, so it can be exercised on synthetic source
# ============================================================

def is_placeholder(account: str) -> bool:
    """Whether this account segment names nobody.

    Three ways to be one, in the order they are cheapest to read: the spelling
    is punctuation a person recognises as a slot, the segment is an elision, or
    the word is on the declared list.
    """
    if _PLACEHOLDER_SHAPE.search(account):
        return True
    if _ELLIPSIS.match(account):
        return True
    return account.casefold() in PLACEHOLDER_ACCOUNTS


def reason_near(lineno: int, lines: list[str]) -> str:
    """The exemption reason on this line or the one directly above it, joined.

    Both positions are read because the two contexts this guard lives in want
    different ones: a `#` comment sits above a Python constant comfortably, and
    a Markdown line or a docstring sentence carries the marker inline.
    """
    parts: list[str] = []
    for index in (lineno - 2, lineno - 1):
        if 0 <= index < len(lines):
            text = lines[index]
            position = text.find(MARKER)
            if position == -1:
                continue
            tail = text[position + len(MARKER):].strip()
            if tail.startswith(":"):
                tail = tail[1:].strip()
            if tail.startswith("("):
                closing = tail.rfind(")")
                tail = tail[1:closing] if closing > 0 else tail[1:]
            parts.append(tail.strip())
    return " ".join(p for p in parts if p).strip()


def findings_in(rel_path: str, text: str) -> list[tuple[str, int, str, str]]:
    """`(path, lineno, the matched path, reason)` for one file's text.

    Pure: takes the file's text, returns findings. Deleting the line that
    appends a finding changes a live result, which is what makes this testable
    on synthetic prose rather than only on the tree.

    A finding is reported when the reason is SHORT OR ABSENT, and the two are
    reported the same way on purpose: an empty marker and no marker at all are
    both "nothing here for a reviewer to read".
    """
    lines = text.splitlines()
    found: list[tuple[str, int, str, str]] = []
    for number, line in enumerate(lines, start=1):
        for match in HOME_RE.finditer(line):
            account = match.group(1)
            if is_placeholder(account):
                continue
            reason = reason_near(number, lines)
            if len(reason) >= MIN_REASON_CHARS:
                continue
            found.append((rel_path, number, match.group(0), reason))
    return found


# ============================================================
# The tree
# ============================================================

def scan(root: Path) -> tuple[list[tuple[str, int, str, str]], int, list[str]]:
    """(findings, files_read, files_not_text). The count feeds the corpus floor."""
    findings: list[tuple[str, int, str, str]] = []
    read = 0
    not_text: list[str] = []
    for rel in git_index_paths(root):
        path = root / rel
        if path.suffix.lower() in BINARY_SUFFIXES:
            not_text.append(rel)
            continue
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            # Tracked but deleted in the working tree. Not a fault, and not a
            # file this guard can have an opinion about.
            continue
        except OSError as exc:
            # Never swallow: an unreadable tracked file is a hole in the sweep,
            # and reporting a clean tree over one is this guard's own defect.
            print(f"{RED}cannot read {rel}: {exc}{RESET}", file=sys.stderr)
            not_text.append(rel)
            continue
        if b"\0" in raw:
            not_text.append(rel)
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            not_text.append(rel)
            continue
        read += 1
        findings.extend(findings_in(rel, text))
    return sorted(findings), read, sorted(not_text)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="exit 1 when any tracked file names a real home directory")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    try:
        findings, files_read, not_text = scan(ROOT)
    except IndexUnreadable as exc:
        message = f"git could not list the tracked tree: {exc}"
        if args.json:
            print(json.dumps({"refused": message}, indent=2))
        else:
            print(f"{RED}{BOLD}REFUSED{RESET} {message}", file=sys.stderr)
        return 2

    listed = files_read + len(not_text)
    if files_read < MIN_CORPUS or files_read < MIN_TEXT_SHARE * listed:
        message = (f"read {files_read} of {listed} tracked files as text, below "
                   f"the floor of {MIN_CORPUS} files or {MIN_TEXT_SHARE:.0%} of "
                   f"the listing; refusing to report a verdict over a corpus "
                   f"this small")
        if args.json:
            print(json.dumps({"refused": message}, indent=2))
        else:
            print(f"{RED}{BOLD}REFUSED{RESET} {message}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({
            "files_read": files_read,
            "files_not_text": len(not_text),
            "findings": [{"path": p, "line": n, "match": m, "reason": r}
                         for p, n, m, r in findings],
        }, indent=2))
        return 1 if (args.check and findings) else 0

    print(f"{BOLD}home-paths{RESET}  {files_read} text file(s) read, "
          f"{len(not_text)} not text, {len(findings)} finding(s)")

    if not findings:
        print(f"{GREEN}OK -- no tracked file names a real home directory.{RESET}")
        print(f"{GRAY}Literals only; a path built at run time is invisible here, "
              f"and the repository's history is out of scope.{RESET}")
        return 0

    print(f"\n{RED}{BOLD}{len(findings)} home directory path(s) in the public "
          f"engine{RESET}")
    for path, lineno, match, reason in findings:
        suffix = f"  {GRAY}(reason too short: {len(reason)} chars){RESET}" if reason else ""
        print(f"  {RED}x{RESET} {path}:{lineno}  {match}{suffix}")

    print(f"\n{YELLOW}This repository is public. A home path publishes the "
          f"account name, the machine's layout and the names of its working "
          f"trees.{RESET}")
    print(f"{YELLOW}Genericize it: an invented account and an invented "
          f"workspace path carrying the same meaning.{RESET}")
    print(f"{YELLOW}If the real path genuinely has to stay, put "
          f"`{MARKER} (<reason>)` on its line or the line above, at least "
          f"{MIN_REASON_CHARS} characters.{RESET}")
    print(f"{GRAY}Do NOT add an account name to PLACEHOLDER_ACCOUNTS to pass "
          f"your own commit.{RESET}")

    return 1 if args.check else 0


if __name__ == "__main__":
    sys.exit(main())
