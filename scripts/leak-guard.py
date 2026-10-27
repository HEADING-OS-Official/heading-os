#!/usr/bin/env python3
"""Leak guard for the HEADING OS engine/data boundary.

Two checks (HEADING OS spec Section 6):
  check-paths   Lint engine source for hardcoded data-path literals authored
                outside the get_*_dir() seam (scripts/utils/workspace.py).
                Since 2026-09-14 the token list also names the nine HEADING OS 2
                family roots, derived from `families.family_roots()` rather than
                typed, because the records under the legacy roots are moving to
                `<family>/<leaf>/<their old path>` and a token list naming only
                the old roots stops seeing each literal on the day it is
                rewritten.
  check-staged  Fail if any staged file routes anywhere but engine. Active
                whenever this clone is the engine (auto-detected).

Usage:
  python scripts/leak-guard.py check-paths  --files a.py b.py
  python scripts/leak-guard.py check-staged --files a.md b.md
"""
import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.heading_os_2.families import NOT_A_RESOURCE_NAME, family_roots, subject_roots
from scripts.utils.data_path import RECORD_SCHEME, RUNTIME_SCHEME, validate_address
from scripts.utils.denial_log import log_denial
from scripts.utils.paths import is_engine_tree
from scripts.utils.workspace import (
    RoutingRefused, get_data_root, get_routing_destination, get_workspace_root,
    routing_verdict,
)

# Data-path tokens that must never be hardcoded as string literals in engine code:
# the roots that resolve to private data, plus the HEADING OS 2 family roots and
# the C3 subject roots, derived from `families` and never typed, so a new root is
# guarded the day it is added (2026-09-14, 2026-10-13). `build/` and `work/` are
# ordinary words: a legitimate literal takes the inline `# leak-guard: ok
# (<reason>)` marker, never a SEAM_ALLOWLIST entry.
#
# Each line below carries the marker because THIS file is scanned for data-path
# references by `scripts/check-data-path-references.py`, which records a marked
# site as intentional. The literal here IS the forbidden token, compared against
# source text and never joined to a root.
DATA_PATH_TOKENS = [
    "crm/contacts",  # leak-guard: ok (the forbidden token itself; compared against source text, never joined)
    "knowledge/odin-brain",  # leak-guard: ok (the forbidden token itself; compared against source text, never joined)
    "outputs/",  # leak-guard: ok (the forbidden token itself; compared against source text, never joined)
    "threads/",  # leak-guard: ok (the forbidden token itself; compared against source text, never joined)
    "datastore/operations/tribe/fireside-state",  # leak-guard: ok (the forbidden token itself; compared against source text, never joined)
] + [f"{fam}/" for fam in (*family_roots(), *subject_roots())]


def runtime_store_tokens() -> list[str]:
    """The runtime store's address prefixes: the engine default's and this machine's registry's.

    Widened 2026-10-13 (HOS-241). The store sits under the data root like every
    record and holds browser cookies, drafted mail and calendar dumps, yet its
    name was in neither list above, so an engine literal spelling
    `"<store>/browser/cache/cookies.json"` passed this gate. Both names, read
    from the registry and never typed: the default for every tree without a
    `runtime_store:` entry, and the registry's while the C3 move carries the
    store into a subject root, so a literal spelling either base is refused on
    both sides of the cut-over. A registry that cannot be read keeps the default
    and says so; the gate never narrows in silence.
    """
    from scripts.utils.paths import DataRootError
    from scripts.utils.runtime_dir import RuntimeDirError, default_store_dirname, store_dirname

    names = [default_store_dirname()]
    try:
        names.append(store_dirname())
    except (DataRootError, RuntimeDirError) as exc:
        print(f"leak-guard: the registry's runtime store name is unreadable ({exc}); "
              f"guarding the default `{names[0]}/` only", file=sys.stderr)
    return list(dict.fromkeys(f"{name}/" for name in names))


DATA_PATH_TOKENS += [t for t in runtime_store_tokens() if t not in DATA_PATH_TOKENS]

# A literal that is a HOS-5 LOGICAL ADDRESS (`record:` / `runtime:`), anchored
# like `_LITERAL_RE`: refused when `validate_address` says it names nothing. `{`,
# `<` and `>` mark a message template rather than an address (MEASURED 2026-09-20).
_ADDRESS_LITERAL_RE = re.compile(r"""['"]((?:%s)[^'"{}<>]*)['"]""" % "|".join(
    re.escape(s) for s in (RECORD_SCHEME, RUNTIME_SCHEME)
))

#: The front door's five operations; `data_write_dir`, the one that creates,
#: joined the three that only answer on 2026-09-25 (HOS-5 § 6), and
#: `data_write_file`, its twin for a name that places one file, followed the
#: same day (HOS-5 § 7). Imported in spirit from the module that owns them
#: rather than re-derived, and asked of the AST below: a REGEX cannot tell
#: `def data_path(name: str, ...)` from a call, and that module defines all
#: five, so any pattern loose enough to catch a variable argument catches the
#: definitions too.
FRONT_DOOR_CALLS = frozenset({"data_path", "data_read", "data_read_required", "data_write_dir",
                              "data_write_file"})

# Files allowed to contain these literals (the seam owns the canonical paths).
# Only .py/.sh members matter — the suffix filter below skips everything else,
# so non-code files do not need listing here (review finding L1).
SEAM_ALLOWLIST = {
    "scripts/utils/workspace.py",
    "scripts/leak-guard.py",
}

# Match a quoted literal that STARTS with a data token — i.e. a path literal
# like "crm/contacts/..." — not a URL or log message that merely CONTAINS the  # leak-guard: ok (scanner vocabulary)
# substring ("https://x/outputs/y", "writing outputs/report"). Anchoring the  # leak-guard: ok (scanner vocabulary)
# token to the opening quote removes the URL / log-message false positives
# (review finding M4). A literal that builds a path from a token still starts
# with it: root / "crm/contacts" -> the literal is "crm/contacts".  # leak-guard: ok (scanner vocabulary)
_LITERAL_RE = re.compile(r"""['"]((?:%s)%s[^'"]*)['"]""" % ("|".join(
    re.escape(t) for t in DATA_PATH_TOKENS
), NOT_A_RESOURCE_NAME))


def _non_literal_front_door_calls(text: str) -> list[tuple[int, str]]:
    """Every `data_path(<not a string literal>)` in one module, with its line.

    The other half of the address check, and the one a pattern over TEXT cannot
    do. `_ADDRESS_LITERAL_RE` above reads an address somebody spelled out and
    asks whether it names anything; this asks whether an address was spelled at
    all. MEASURED 2026-09-20 against the tree: `data_path(name)` was reported by
    nothing, here or in `scripts/utils/runtime_write_guard.py`, and that is the
    shape HOS-5 phase 3 produces in quantity, because a repointed caller holds
    its address in a variable more often than in a quote.

    Asked of the AST for two reasons that are not taste. A regex cannot tell
    `def data_path(name: str, ...)` from a call, and the module that defines all
    four of these is the module a loose pattern would flag hardest. And a call
    split across lines arrives here whole.

    Returns the line the CALL starts on, which is where the sanctioned
    `# leak-guard: ok (<reason>)` marker has to go. The front door's own CLI is
    the one place in this engine where the address legitimately arrives from
    outside as a value: it carries that marker rather than a file-level
    exemption, because an allowlisted file stops being checked at all.

    A file that does not parse yields the empty list, matching
    `_docstring_lines`: the line scan still runs over it, so the file is not
    silently dropped, only this half of it.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    lines = text.splitlines()
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name not in FRONT_DOOR_CALLS:
            continue
        arg = node.args[0] if node.args else next(
            (kw.value for kw in node.keywords if kw.arg == "name"), None)
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            continue  # a literal: `_ADDRESS_LITERAL_RE` and `validate_address` have it
        lineno = getattr(node, "lineno", 0)
        source = lines[lineno - 1].strip() if 0 < lineno <= len(lines) else ""
        found.append((lineno, source))
    return found


def _docstring_lines(text: str) -> set[int]:
    """Line numbers inside a module, class or function DOCSTRING.

    A docstring is prose. It routes nothing, opens nothing, and joins nothing
    to a root, so a data path spelled in one is a citation rather than an
    address. The line scan below cannot tell the two apart, and the `#`
    exemption above does not reach a docstring because a comment cannot be
    appended to a line inside a triple-quoted string.

    MEASURED 2026-09-18: the migration repairs of that day record their
    evidence the way `.claude/rules/development-standards.md` obligation 2
    requires, by quoting the classifier call that established the kind, for
    example `manifest.classify` of the pre-move path of the `reminders` record.
    13 such citations in 13 files were refused as hardcoded addresses, and the
    only ways to pass were to delete the evidence or to widen the file-level
    `SEAM_ALLOWLIST`, which would have stopped checking those files entirely.

    Scoped to docstrings and NOT to every string constant on purpose. A path
    assigned to a name, passed to a call or joined to a root is exactly what
    this gate exists to refuse, and all of those are string constants too.
    Both halves were held by a test until the 2026-09-21 test-suite split: the
    citation passes, the assignment one line below it does not. Nothing runs
    that pair now, so widening this scope past docstrings would go unreported
    until somebody read the diff.

    A file that does not parse yields the empty set rather than raising, so an
    unparseable file is scanned WHOLE and over-reports. That direction is the
    deliberate one: this gate's own history is a control whose failure was
    indistinguishable from its success.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            lines.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return lines


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(get_workspace_root()))
    except ValueError:
        return str(path)


# A path whose destination this gate knows without asking: it is engine code by
# definition, since it IS the engine's leak gate. Used only to ask the classifier
# a question whose right answer is already known.
_CLASSIFIER_CANARY = "scripts/leak-guard.py"


def _classifier_is_answering() -> bool:
    """True when `get_routing_destination` still recognises engine code.

    `load_routing_map()` fails CLOSED: an unreadable, non-UTF-8, or malformed
    `config/routing-map.yaml` classifies EVERY path 'private'. That is the right
    direction for `check_staged`, which then refuses everything. Composed with
    the `!= "engine"` skip in `check_paths` it is the WRONG direction: every file
    is skipped, no literal is ever examined, and the gate returns 0 - which is
    byte-for-byte what a clean tree looks like.

    MEASURED 2026-09-01 on one engine file holding a real violation
    (a constant `P` assigned a literal path into the legacy CRM contacts
    directory):

        healthy map   -> 1 violation, BLOCKED
        degraded map  -> 0 violations, silence          <- the defect

    At commit time the sibling `leak-guard-staged` hook masks it, because a
    degraded map makes it refuse every staged path. Nothing masks it in CI:
    `.github/workflows/ci.yml` runs `check-paths` over `git ls-files` with no
    `check-staged` beside it, so the whole-tree lint would pass having read
    nothing. Same shape as the unreadable-file skip fixed the same day, one
    level up: a control whose failure is indistinguishable from its success.
    """
    return routing_verdict(_CLASSIFIER_CANARY, tree="engine") == "engine"


def check_paths(files) -> int:
    if not _classifier_is_answering():
        log_denial(mechanism="leak-guard:check-paths", action="commit",
                   path=_CLASSIFIER_CANARY,
                   reason="routing map degraded, so nothing was checked")
        print("BLOCKED - the routing map is not classifying engine code, so this "
              "gate checked nothing:")
        print(f"  config/routing-map.yaml answers "
              f"'{routing_verdict(_CLASSIFIER_CANARY, tree='engine')}' for "
              f"{_CLASSIFIER_CANARY}, which is engine code by definition.")
        print("  The map failed closed to 'private' for every path, which makes "
              "this lint skip every file and report clean. Fix the map "
              "(unreadable? not UTF-8? `rules:` written as a list?) and re-run.")
        return 1
    violations = []
    # Paths this gate could NOT read. They BLOCK: until 2026-09-01 an unreadable
    # file was skipped and the gate returned 0, exactly like a clean one.
    unreadable = []
    # Paths that vanished between the listing and the read AND are not staged,
    # so they carry nothing into this commit. Reported, never blocking - see the
    # FileNotFoundError branch below for why the staged half is not in here.
    vanished = []
    for f in files:
        p = Path(f)
        rel = _rel(p)
        if rel in SEAM_ALLOWLIST:
            continue
        if p.suffix not in {".py", ".sh"}:
            continue
        # Only lint actual engine code. Test files legitimately embed data-path
        # literals as fixtures; archived scripts under scripts/archive/ are inert
        # dead code (never run, retained for history); and a .py that itself routes
        # anywhere but engine (e.g. a throwaway build script inside outputs/) is
        # not shippable engine code — linting any of these for "engine must not
        # hardcode data paths" is wrong.
        if (
            rel.startswith("tests/")
            or rel.startswith("scripts/archive/")
            or routing_verdict(rel, tree="engine") != "engine"
        ):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except FileNotFoundError:
            # Walk-then-read race. Unlike `scripts/push-all.py`, which re-stages
            # with `git add -A`, nothing re-stages here: a tracked file deleted
            # from the worktree still carries its content in the INDEX. So the
            # index is asked: still staged BLOCKS as unreadable; absent from the
            # worktree AND the index carries nothing and is only reported.
            if _staged_in_index(p):
                unreadable.append(
                    (rel, "FileNotFoundError: gone from the worktree but still "
                          "staged, so its content is in this commit"))
            else:
                vanished.append(rel)
            continue
        except (OSError, UnicodeDecodeError) as exc:
            unreadable.append((rel, f"{type(exc).__name__}: {exc}"))
            continue
        prose = _docstring_lines(text)
        for n, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if n in prose:
                continue
            # Inline suppression for legitimate relative-path usages (comparison
            # keys, prefix patterns, f-string display, log/error/regex strings)
            # that are NOT absolute-path construction. Annotate with a reason:
            #   "crm/contacts/",  # leak-guard: ok (relative prefix match)
            if "leak-guard: ok" in line:
                continue
            m = _LITERAL_RE.search(line)
            if m:
                violations.append((rel, n, m.group(1)))
                continue
            address = _ADDRESS_LITERAL_RE.search(line)
            if address:
                why = validate_address(address.group(1))
                if why is not None:
                    violations.append((rel, n, f"{address.group(1)} -- {why}"))
        if p.suffix == ".py":
            for n, source in _non_literal_front_door_calls(text):
                if "leak-guard: ok" in source or n in prose:
                    continue
                violations.append((rel, n, (
                    f"{source} -- the address is not a literal, so no gate can "
                    f"say which data it names. Spell the name at the call")))
    if violations:
        for rel, n, lit in violations:
            # The TOKEN, never the matched literal. `_LITERAL_RE` captures the
            # token plus everything up to the closing quote, so the literal can
            # carry a real path tail ("outputs/clients/<name>-contract.pdf") —  # leak-guard: ok (scanner vocabulary)
            # the refused content itself, which `denial_log` states a record
            # never carries and which `redact()` does not strip because a path
            # is not credential-shaped. content-guard and the push walls already
            # log only the class; this now matches them.
            token = next((t for t in DATA_PATH_TOKENS if lit.startswith(t)), "unknown")
            log_denial(mechanism="leak-guard:check-paths", action="commit",
                       path=f"{rel}:{n}", reason=f"hardcoded data-path literal [{token}]")
        print("BLOCKED - hardcoded data-path literal(s) outside the get_*_dir() seam:")
        for rel, n, lit in violations:
            print(f"  {rel}:{n}  \"{lit}\"  -> use a get_*_dir() helper from scripts/utils/workspace.py")
    if unreadable:
        # Refuse, rather than report a scope this gate never assembled. The
        # reason is only NOT to say "clean": an engine file that could not be
        # opened has not been checked, and the commit is the moment to say so.
        # Denials are logged with the class only, matching the violation branch
        # above, since a path is not credential-shaped and `redact()` would not
        # strip one.
        for rel, why in unreadable:
            log_denial(mechanism="leak-guard:check-paths", action="commit",
                       path=rel, reason=f"unreadable, so unchecked [{why.split(':')[0]}]")
        print("BLOCKED - engine file(s) this gate could not read, so did not check:")
        for rel, why in unreadable:
            print(f"  {rel}  -> {why}")
        print("  Fix the file, or take it out of the commit. A file the gate "
              "cannot read is not a file the gate found clean.")
    if vanished:
        # Not a block. There is no content here for the commit to carry, so
        # refusing would stop a commit over the gate's own timing. It is still
        # SAID, because a corpus that shrank in silence is a narrowed check
        # printing like a complete one (`.claude/rules/scope-claims.md`).
        print(f"leak-guard: {len(vanished)} path(s) vanished between the "
              f"listing and the read and are not staged, so they carry nothing "
              f"into this commit and were not checked:")
        for rel in vanished:
            print(f"  {rel}")
    if violations or unreadable:
        return 1
    return 0


def _staged_in_index(p: Path) -> bool:
    """Is there content for this path in the index right now?

    The question the FileNotFoundError branch above has to answer: a file gone
    from the worktree is harmless only if it is gone from the commit too.

    Fails CLOSED. A git call that cannot answer returns True, so the path is
    treated as still-staged and BLOCKS. "I could not tell" must never resolve to
    "nothing was there" in a gate whose whole job is refusing.
    """
    try:
        rel = p.resolve().relative_to(get_workspace_root().resolve()).as_posix()
    except (ValueError, OSError):
        return True
    try:
        proc = subprocess.run(
            ["git", "-C", str(get_workspace_root()), "ls-files", "-z", "--", rel],
            capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return True
    # `ls-files`, not `cat-file -e`: cat-file answers "absent" and "git broke"
    # with the same rc 128; ls-files puts the verdict on stdout and a non-zero
    # exit means only "git did not answer", which fails closed (MEASURED 2026-09-01).
    if proc.returncode != 0:
        return True
    return bool(proc.stdout.strip())


def _in_engine_repo() -> bool:
    """True when this clone is the split-topology engine (data lives in a sibling).

    Auto-detected from the data-root seam: when get_data_root() resolves to a
    DIFFERENT path than the workspace root, we are in the two-part topology and the
    working tree is the engine -- which must stay code-only. The legacy
    HEADING_OS_ENGINE_REPO=1 env var still forces-on as an explicit override, but is
    no longer the SOLE trigger: relying on a hand-set env var is exactly why this
    guard sat inert while four private specs leaked (2026-06-22). Pre-cutover single
    repo (data_root == workspace_root) -> inert, since data is legitimately tracked.

    A tree carrying the engine marker (`is_engine_tree`) is the engine whatever
    the data root says, and is asked FIRST. Until 2026-09-28 the data-root
    comparison was the only test, so a `knowledge/` directory inside the engine,
    or a `HEADING_OS_DATA` naming the engine checkout, made the two roots equal
    and switched `check-staged` off with nothing said.
    """
    import os

    if os.environ.get("HEADING_OS_ENGINE_REPO") == "1":
        return True
    try:
        if is_engine_tree(get_workspace_root()):
            return True
        return get_data_root() != get_workspace_root()
    except Exception:
        # Fail-closed: if the seam cannot resolve, assume engine and enforce.
        return True


def check_staged(files) -> int:
    """Fail if a staged file routes anywhere but `engine`, but only in the engine repo.

    The test is `!= "engine"` rather than membership of a refused set. Until
    2026-09-25 it was `in {"private", "corporate"}`, so an answer nobody had
    listed was committed. HOS-132 retired `corporate` and turned the test round.

    Active whenever this clone is the split-topology engine (auto-detected via the
    data-root seam, or forced by HEADING_OS_ENGINE_REPO=1). Inert on a pre-cutover
    single repo where data files are legitimately tracked.
    """
    if not _in_engine_repo():
        return 0
    leaked = []
    for f in files:
        rel = f.replace("\\", "/").lstrip("/")
        # Asked as an ENGINE path: that is what a file staged here is. A
        # refusal is the contract's third answer (HOS-232) and is reported as
        # what it is, never as 'private'.
        try:
            dest = get_routing_destination(rel, tree="engine")
        except RoutingRefused as exc:
            leaked.append((rel, f"refused ({exc.reason})"))
            continue
        if dest != "engine":
            leaked.append((rel, f"routes {dest}"))
    if leaked:
        for rel, why in leaked:
            log_denial(mechanism="leak-guard:check-staged", action="commit",
                       path=rel, reason=why)
        print("BLOCKED - non-engine content staged into the engine repo:")
        for rel, why in leaked:
            print(f"  {rel}  -> {why}; only an allowlisted engine file may be committed here")
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="HEADING OS leak guard")
    sub = ap.add_subparsers(dest="cmd", required=True)
    cp = sub.add_parser("check-paths")
    cp.add_argument("--files", nargs="*", default=[])
    cs = sub.add_parser("check-staged")
    cs.add_argument("--files", nargs="*", default=[])
    args = ap.parse_args()
    if args.cmd == "check-paths":
        return check_paths(args.files)
    if args.cmd == "check-staged":
        return check_staged(args.files)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
