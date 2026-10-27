#!/usr/bin/env python3
"""Engine references into the DATA overlay, checked against the overlay itself.

The engine is mostly PROSE. A skill that says "save it to `outputs/documents/`", (leak-guard: ok, scanner vocabulary)
a rule naming `crm/contacts/`, an agent brief naming `knowledge/odin-brain/` -- (leak-guard: ok, scanner vocabulary)
none of that is code, and none of it breaks when the directory it names stops
existing. A path joined onto the data root happily recreates the tree and the
file lands in a directory nobody reads any more. `python -m pytest` compiles code; it has never
read a sentence. So a move of the data overlay (the nine-family migration of
2026-09 was one) can silently orphan an artifact stream, and the first symptom
is a file somebody cannot find months later.

This scanner makes that loud. It extracts every DATA-relative path the engine
names, resolves it against the real overlay, and refuses to report a pass over
anything it could not establish.

WHY AN `exists()` CHECK WOULD BE WORSE THAN NOTHING
===================================================
The migration strategy is "label and verify first, move files later", so the new
families are created BEFORE anything moves into them. For the whole length of
that transition the new directories exist and are EMPTY. A guard whose test is
`(data_root / path).exists()` answers True for every one of them, prints a clean
tree, and stops being a guard at precisely the moment it is needed -- while
reading exactly like a guard that checked something. A green is evidence to
whoever reads it, which makes the silent version worse than no version.

Then a second floor went in under the first. Each of the nine family roots
carries a `README.md` saying it is empty on purpose, so the obvious repair --
"the directory holds at least one file" -- is defeated too, by the very file
that announces the emptiness.

Existence is therefore never the test. Three outcomes, and two of them fail:

  ok           a file, or a directory holding content.
  missing      nothing is at that path. A defect TODAY, and it announces itself.
  unpopulated  the path is THERE, correctly named, and holds no content. A
               defect DURING THE TRANSITION, and it announces nothing, which is
               what makes it the worse of the two.

Two independent mechanisms answer `unpopulated`, and the authoritative one is
asked first. The migration manifest `family-locations.tsv` carries a `state`
per target, and exactly ONE of its values is read here: a reference into a
`not-moved` target is unpopulated BY
DEFINITION, with no filesystem question asked, so whatever the tree gets seeded
with cannot turn the answer green. Where no manifest row covers the path, the
disk decides, and there a lone root `README.md` counts as the sign it is rather
than as content. Both are kept: the manifest can go stale and the disk cannot
see intent, so neither alone is enough, and the mechanism that answered travels
out with the verdict instead of being inferred from it.

`unpopulated` is not a warning and not a sub-case of `ok`. It is counted, listed
in its own section, and exits non-zero, so standing the new tree up makes this
scanner LOUDER rather than quieter. That inversion is the whole reason the file
exists. It was held by a run that included a mutation planting `return True` in
`_populated()` and asserting the refusal disappeared with it, and that run was
parked in the 2026-09-21 test-suite split. The inversion is still coded below;
nothing now notices if `_populated()` starts saying yes to everything.

The same refusal runs one level up, on the corpus itself. Zero engine files
read, zero references extracted, zero references in scope, an absent overlay, or
a routing map that cannot be parsed: each returns exit 2, never 0 and never 1.
0 means every reference resolved and 1 means one did not; a scanner that
inspected nothing has established neither.

WHAT IS IN SCOPE, AND THE FALSE POSITIVE THAT DOMINATES EVERYTHING ELSE
=======================================================================
`config/`, `docs/`, `reference/`, `scripts/` and `tests/` are top-level
directory names on BOTH sides of the seam, measured 2026-09-12 by intersecting
the two trees (a sixth, the legacy admin directory, has since been removed from
the data repository). `docs/DEPLOYMENT.md` is engine and `docs/design/` is overlay;
the operator's voice guide under `reference/` is overlay and
`reference/vpn-preflight.md` is engine.
A prefix list cannot tell them apart, and this is a larger false-positive source
than every docstring example in the tree put together.

So no prefix list decides scope. Two clauses do, in this order:

  1. The resolver is asked PER PATH, and a `private` answer puts the path in
     scope. Until HOS-232 this read "an explicit `private` rule", and until
     2026-09-25 an explicit `corporate` rule took a path OUT (HOS-132 retired
     that destination).
  2. Failing that, the first segment is checked against the set of top-level
     names the OVERLAY has and this checkout does not.

Clause 2 is not a convenience. On 2026-09-12 the routing map had no rule for any
of the nine new families, so `canon/`, `ledger/`, `trace/` and their siblings all
resolved through `default: engine` and a scope built on rules alone was blind
exactly over the tree this scanner exists to watch. Since HOS-232 (2026-10-12) the data
repository is private by repository and the map carries no data rule at all,
so clause 1 reads "the resolver answers `private`", and the names clause 2
cannot reach, legacy roots from before the HEADING OS 2 move and names both
trees use, come from the overlay's own list (`legacy_data_addresses`). Without that
list the scope fell from 200 missing references to 46, MEASURED 2026-10-12, so
its absence is a refusal and never a smaller pass.

A path that neither clause claims belongs to `scripts/check-path-references.py`,
the sibling that checks the engine tree from the other side.

The remaining failure mode, stated rather than left to be found: a `private`
rule DELETED takes its subtree out of scope silently, and clause 2 does not
cover it for the six names the two trees share. Nothing here detects that. The
per-subtree counts in the report and the `in_scope == 0` refusal are what make a
collapse visible, and neither would catch one subtree out of eight going quiet.
A stated limit, not a claim of coverage.

EXTRACTION, AND WHAT IT MISSES
==============================
Python is read through the AST: every `str` constant, including f-string parts
and docstrings, matched against the anchor pattern. Not a text scan, so a fix
that quotes a bad pattern in a comment to explain it does not go red.

Everything else -- Markdown, YAML, JSON, shell, HTML -- is read as PROSE, line by
line, through the same anchor pattern. The brief asked for Markdown; the pattern
costs nothing on the other formats and `config/` is mostly YAML and JSON, so
narrowing it to `.md` would have dropped the half of `config/` that names a path.

Both halves are heuristic in both directions. A path spelled unusually is missed.
A sentence continued past what looked like a path is captured (`outputs/x` out of (leak-guard: ok, scanner vocabulary)
`outputs/x_and_more`). The intentional ones live in BASELINE with a mandatory (leak-guard: ok, scanner vocabulary)
reason, so what this tool ignores is readable rather than silently filtered.

One structural exception to the prose read, HOS-127, MEASURED 2026-09-25: in
the registry template `scripts/data-names.example.yaml` the value of every
`names.<entry>.under` key (and, since HOS-240 on 2026-10-13, every
`names.<entry>.path` key, for the reason at `_TEMPLATE_MASKED_FIELDS`) is
blanked before the scan, because that field is a
record's PRE-MOVE spelling (or a sub-address relative to its base), never a live
root-relative address. It had put 51 of 147 entries into `new`. The file is
composed as YAML to find those values; nothing else in it, and nothing in any
other file, is exempted. `mask_registry_history()` carries the detail. The
operator's live registry is not in this scanner's corpus at all: it lives under
the data root, and the corpus is git-tracked engine files. The services template
`scripts/runtime-services.example.yaml` has no field of this kind; its values
are one-line descriptions and are read as prose like any other.

A DIRECTORY WHOSE NAME CONTAINS A SPACE IS NOT COVERED BY ANY PIN
=================================================================
MEASURED 2026-09-14: the prose anchor stops at the space, so
`outputs/client reports` is extracted as `outputs/client`, and a pin on (leak-guard: ok, scanner vocabulary)
`outputs/client` does not cover the directory that was actually named. Records (leak-guard: ok, scanner vocabulary)
under such a directory would move while an engine literal still names the old
place, which is the exact failure this scanner exists to make loud. The same
truncation applies to a path broken across a newline: `outputs/customer\nnotes` (leak-guard: ok, scanner vocabulary)
is extracted as `outputs/customer`. (leak-guard: ok, scanner vocabulary)

This paragraph is where that finding lives, and it is the only place it lives.
It was once recorded in a file kept under `pytest.mark.xfail(strict=True)`, so
that fixing the gap would make the marker fail and force the file's removal.
MEASURED 2026-09-18 with `pytest --runxfail`: that signal could not fire, because
the same file also asserted the conservative `role()` policy the branch replaced
on 2026-09-14, so 22 of its 33 readers failed on the settled policy rather than
on this gap and no repair of the gap alone could produce an XPASS. The file was
deleted in the test-value audit of 2026-09-18; the classifier that replaced its
policy was parked in the 2026-09-21 test-suite split. Nothing anywhere signals
this gap now, so read the truncation rule above before trusting an extraction.

INTENTIONAL PATHS
=================
One marker, the one this tree already uses: a line carrying the substring
`leak-guard: ok` is skipped; 95 sites across the tree, measured 2026-09-12. No
second marker is invented here, because two markers for one idea is two things
to keep in sync and the second one is always the one that stops being applied.

Usage:
  python scripts/check-data-path-references.py                # human report
  python scripts/check-data-path-references.py --check        # exit 1 on a NEW unresolved
  python scripts/check-data-path-references.py --json         # machine-readable
  python scripts/check-data-path-references.py --write-report # + the JSON report under the registry name ho2-migration-reports

Coverage: none live since the 2026-09-21 test-suite split. This scanner still
runs as a gate; what stopped is everything that checked the scanner itself.
"""
import argparse
import ast
import io
import json
import os
import re
import sys
import tokenize
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.workspace import (  # noqa: E402
    RoutingRefused, get_workspace_root, get_routing_destination, get_default_tz,
    load_routing_map,
)
from scripts.utils.paths import get_data_root, data_root_is_demo  # noqa: E402
from scripts.utils.repo_files import git_index_paths, IndexUnreadable  # noqa: E402
from scripts.utils.atomic import atomic_write_text  # noqa: E402
from scripts.utils.colors import GREEN, YELLOW, RED, CYAN, GRAY, BOLD, RESET  # noqa: E402
from scripts.utils.statement_exemptions import (  # noqa: E402
    ExemptionsError, line_key, load_exemptions, match as match_exemptions)
from scripts.heading_os_2.families import NOT_A_RESOURCE_NAME  # noqa: E402
from scripts.heading_os_2.resolve import (  # noqa: E402
    LOCATIONS_FILENAME, STATE_NOT_MOVED, NoManifestOnThisTree, manifest_dir,
)
from scripts.utils.data_path import (  # noqa: E402
    EXAMPLE_NAMES_REL, RECORD_SCHEME, RUNTIME_SCHEME, DataPathError,
    addresses_in, data_path,
    validate_address,
)

# ============================================================
# Configuration
# ============================================================

# The engine subtrees this scanner reads. Every one of them is prose-heavy or
# config-heavy; `scripts/` and `tests/` are here for their string literals.
SUBTREES = (
    "scripts/",
    "tests/",
    ".claude/skills/",
    ".claude/rules/",
    ".claude/hooks/",
    "config/",
    "docs/",
)

# The marker this tree already uses for a path literal that is deliberately not
# a path reference (a comparison key, a prefix pattern, a regex fragment).
# `scripts/leak-guard.py` defines it; adopting it rather than inventing a second
# one is a deliberate choice -- see the module docstring.
INTENTIONAL_MARKER = "leak-guard: ok"

# Where the report lands when `--write-report` is passed: the registry name
# `ho2-migration-reports`, asked at the call in `main()` and never spelled out
# here. `--write-report` is off every automated path (CI and
# `scripts/push-all.py` never pass it), and a tree whose registry does not
# carry the name refuses the write by name rather than inventing the address.

# The scanner's own scope input, read back as a corpus. See `_RULE_DECLARATION`.
ROUTING_MAP_REL = "config/routing-map.yaml"

# A frozen ratchet with MANDATORY reasons, the same shape as
# `scripts/check-path-references.py`'s. An entry is a data-relative path that
# does not resolve and SHOULD NOT: a placeholder, an example, or correct prose
# about something retired. An entry without a reason is indistinguishable from a
# finding somebody gave up on, so the value is never allowed to be empty.
#
# Frozen empty on 2026-09-12: this scanner is new, nothing has been triaged yet,
# and pre-loading it with today's findings would hide the very list the
# migration needs to work through. Populate it as each finding is judged
# intentional, one line and one reason at a time.
BASELINE: dict[str, str] = {
    # The migration lock exists only between a move's first rename and the
    # manifest publish (MIGRATION_LOCK, `scripts/heading_os_2/manifest.py`). Its
    # absence is the healthy state, so "missing" is the right answer and not a
    # defect. Added 2026-09-14 with the lock itself.
    "_manifest/MIGRATION-IN-PROGRESS": "a lock held only while a move is running; absent is healthy",
    #
    # Every key in this table is a literal that IS a data token, compared
    # against source text and never joined to a root, so each line carries
    # `scripts/leak-guard.py`'s sanctioned marker where the guard refuses its
    # token. A key matches the spelling the ENGINE PROSE uses, never an address
    # anything opens, so it is never built from a resolver: a key that tracked
    # the file would stop matching the sentence and suppress nothing.
    # Rows leave on the scanner's own `stale_baseline` report; their history is
    # in `git log -p` of this file.
}

# The COUNT ratchet, which is a different instrument from BASELINE above and is
# deliberately not the same file.
#
# BASELINE names paths that should never resolve, one line and one reason at a
# time, and was frozen EMPTY on 2026-09-12 so that pre-loading it could not hide
# the worklist. Read the current count with
# `python scripts/check-data-path-references.py --json` (`len(report["new"])`),
# never from prose.
#
# So the automatic gate counts instead of naming. It stores one integer, fails
# when the number of unresolved paths RISES, and says so when the number falls
# so the floor can be lowered. That makes the debt monotone without pretending
# any of it is intentional, which is exactly what BASELINE would have claimed.
RATCHET_REL = "config/data-path-ratchet.json"

#: Key inside that file. One integer, not a structure: a ratchet with fields
#: invites a second number that nothing compares.
RATCHET_KEY = "unresolved_paths_max"


class Ratchet(NamedTuple):
    """One shrink-only ceiling: where it is stored, and what its integer counts.

    A NamedTuple rather than a second copy of `read_ratchet`/`write_ratchet`,
    because this repository's named dominant defect is one decision in N copies
    and a ratchet is a decision with three moving parts (the file, the key, the
    refusal message). Two ratchets sharing one mechanism cannot drift in how
    they refuse; two hand-written ones can, and the one nobody touched that week
    is the one that stops refusing.

    Two FILES rather than two keys in one, for the reason `RATCHET_KEY` states
    above: the integer is the whole content, so a reader opening either file sees
    one number and the sentence saying what it counts. Both are compared by code
    below, so neither is a number nothing compares.
    """

    rel: str
    key: str
    counts: str     # the sentence stored in the file, and printed on a refusal
    noun: str       # what one unit IS, for the human messages
    set_with: str   # the flag that sets this floor, so the refusal names the
                    # right one. Two ratchets sharing one message is how a
                    # refusal ends up telling the reader to run a command that
                    # writes the OTHER file.


#: The debt ratchet. Counts data paths the engine NAMES that do not resolve.
UNRESOLVED_RATCHET = Ratchet(
    rel=RATCHET_REL,
    key=RATCHET_KEY,
    counts=("the number of data paths named by engine files that do not "
            "resolve against the private overlay and are not in BASELINE. "
            "Lower it whenever the real number falls; never raise it."),
    noun="unresolved data path",
    set_with="--write-ratchet",
)

#: The HOS-5 § 6 PROHIBITION: **no hard-coded Data Repository address in engine
#: Python, except the single registry entry point and statements exempted one
#: at a time, each with a written reason.** Operator, 2026-09-25: "old way
#: should go out and do not stay in the workspace."
#:
#: What it counts is what `Reference.pins` calls pinning: a live path literal,
#: role `path`, outside `tests/`, not a `record:`/`runtime:` name. A docstring, a
#: message the role finder recognises, a comparison key it recognises and a
#: fixture under `tests/` are not built-in addresses, and each exclusion is
#: argued where it lives, at `Reference.pins`.
#:
#: It replaced a shrink-only COUNT ratchet (`config/data-path-pins-ratchet.json`,
#: 2026-09-21 to 2026-09-25), which refused growth and allowed every site that
#: was already there. MEASURED 2026-09-25 on main at 5c19d184: that ratchet
#: read 72 against a ceiling of 81, and 196 further literals of the same role
#: sat on lines carrying `leak-guard: ok`, which dropped them from every count
#: here without a reason being required or any check that the line still
#: needed it. So the marker no longer answers this question: it is
#: `scripts/leak-guard.py`'s suppression, for ITS question, and it still
#: silences the unresolved-path count. Here only an entry in
#: `PINS_EXEMPTIONS_REL` does, keyed per statement (`PinFinding`), with a reason
#: of at least `statement_exemptions.REASON_MIN_WORDS` words; an entry that
#: matches no statement is stale and refused, so the file cannot outlive its
#: code. No file and no directory can be exempted: there is no field for one.
#:
#: The single permitted entry point is `REGISTRY_REL` in
#: `scripts/utils/registry_bootstrap.py`, the registry's own address, which
#: `scripts/utils/data_path.py` re-exports. Until HOS-232 it needed no entry,
#: because no `private` rule claimed `config/data-paths.yaml`; since then scope
#: is the resolver's answer, the file exists only in the data repository, and it
#: carries a reviewed entry like any other literal.
PINS_EXEMPTIONS_REL = "config/data-path-pins-exemptions.json"


class PinFinding(NamedTuple):
    """One statement in engine Python that spells a Data Repository address.

    The unit of review is the STATEMENT, not the literal and not the line: a
    table of twelve legacy spellings is one decision, and a line number moves
    on every edit above it. `text` is every source line the statement spans (a
    compound statement contributes its header only; see `_statement_span`), and
    the exemption key is `statement_exemptions.line_key(text)`.
    """

    file: str
    start: int
    end: int
    text: str
    paths: tuple[str, ...]

    @property
    def key(self) -> tuple[str, str]:
        return self.file, line_key(self.text)


def pin_findings(engine_root: Path, result: "ScanResult") -> list[PinFinding]:
    """Every pinning statement, marked line or not, one finding per statement."""
    by_stmt: dict[tuple[str, int, int], set[str]] = {}
    for r in list(result.references) + list(result.marked):
        if r.kind == "python-literal" and r.pins:
            start, end = r.span if r.span != (0, 0) else (r.line, r.line)
            by_stmt.setdefault((r.file, start, end), set()).add(r.path)
    out: list[PinFinding] = []
    cache: dict[str, list[str]] = {}
    for (rel, start, end), paths in sorted(by_stmt.items()):
        if rel not in cache:
            cache[rel] = (engine_root / rel).read_text(encoding="utf-8").splitlines()
        text = _code_only(cache[rel][start - 1:end])
        out.append(PinFinding(rel, start, end, text, tuple(sorted(paths))))
    return out


def _code_only(lines: list[str]) -> str:
    """The statement's lines with every comment removed and comment-only lines dropped.

    What a reviewer exempts is the CODE: a reworded comment is not a new
    address, so it must not retire the review, and the exemptions file must not
    carry prose the other gates then read as a second copy of it (MEASURED
    2026-09-25: a quoted comment naming `get_outputs_dir()` raised
    `scripts/check-legacy-outputs-sites.py` from 28 to 29). Comments are found
    by the tokenizer, never by searching for `#`, which also opens a string.
    A span that does not tokenize on its own is keyed as it stands.
    """
    cut: dict[int, int] = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO("\n".join(lines) + "\n").readline):
            if tok.type == tokenize.COMMENT:
                cut[tok.start[0] - 1] = tok.start[1]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return "\n".join(lines)
    kept = [(ln[:cut[i]] if i in cut else ln).rstrip() for i, ln in enumerate(lines)]
    return "\n".join(ln for ln in kept if ln.strip())


# ============================================================
# Refusals
# ============================================================

class ScopeCollapsed(RuntimeError):
    """The scanner could not establish a corpus, so it must not report a pass.

    Raised instead of returning an empty result. Every caller either handles
    this or dies on it; neither outcome can be mistaken for a clean tree. The
    message names what collapsed and where to look.
    """


# ============================================================
# Roots -- derived from THIS checkout, never from a constant
# ============================================================

def engine_root_from_checkout() -> Path:
    """The engine tree this script is part of, derived from its own location.

    `scripts/check-data-path-references.py` -> `scripts/` -> the checkout. This
    is the only derivation that is correct in a worktree, which is where a guard
    gets written and therefore where it first runs. A constant, the common git
    directory and `${CLAUDE_PROJECT_DIR}` all name the MAIN clone from inside a
    yard, so a guard armed from any of them inspects a tree the author is not
    editing and passes over the change that needs checking.
    """
    return Path(__file__).resolve().parent.parent


def require_map_tree_agrees(engine_root: Path) -> None:
    """Refuse when the routing map read is not this tree's routing map.

    `matched_routing_rule()` resolves its map under `get_workspace_root()`,
    which honours the `WORKSPACE_ROOT` environment variable before it walks for
    markers. A `.env` copied into a worktree with that variable set therefore
    points every routing question at the MAIN clone while this scanner reads the
    worktree's files -- the two halves answering about different trees, with
    nothing said. The disagreement is cheap to detect and impossible to reason
    about afterwards, so it refuses here rather than producing a report whose
    scope came from somewhere else.
    """
    map_tree = get_workspace_root().resolve()
    if map_tree != engine_root.resolve():
        raise ScopeCollapsed(
            f"the routing map would be read from {map_tree} while the files "
            f"scanned are under {engine_root}. Those are different trees, so "
            f"the scope of this scan would not be the scope it reports. "
            f"WORKSPACE_ROOT is the usual cause; unset it, or point it here.")


def require_data_root(explicit: Path | None) -> Path:
    """The overlay to resolve against, or `ScopeCollapsed` naming why not.

    An absent overlay is the normal state of a public clone and of CI, and it is
    NOT a tree full of missing paths: it is a question this scanner cannot
    answer. Reporting every reference as `missing` there would bury a real
    finding in several hundred false ones, and exiting 1 would make the gate
    meaningless everywhere it runs without the operator's data. So it refuses,
    with exit 2, which says "I established nothing" rather than "I found
    something".
    """
    if explicit is not None:
        root = explicit.resolve()
        if not root.is_dir():
            raise ScopeCollapsed(f"--data-root {root} is not a directory")
        return root
    if data_root_is_demo():
        raise ScopeCollapsed(
            "the data root is the demo stand-in, so there is no overlay to "
            "resolve these references against. This scanner needs the real "
            "private overlay; it has nothing to say on a public clone.")
    root = get_data_root()
    if not root.is_dir():
        raise ScopeCollapsed(
            f"the data root {root} does not exist, so no reference could be "
            f"resolved. Set HEADING_OS_DATA, or run this where the overlay is.")
    return root


def require_routing_map(engine_root: Path, data_root: Path) -> dict:
    """This tree's routing rules, or `ScopeCollapsed` naming the file.

    `data_root` decides which overlay contributes its half of the map, so the
    rule set is a function of the arguments rather than of `HEADING_OS_DATA`.
    That is why this is now called AFTER `require_data_root` in `main` and not
    before it; `in_data_scope` carries the measurement.

    `load_routing_map()` fails CLOSED and SILENTLY: an absent, unreadable or
    unparseable map returns `{"default": "private", "rules": {}}`, which is
    indistinguishable from a real map with no rules. That is right for the leak
    wall and wrong here, because this scanner's scope is exactly "the paths an
    explicit rule sends to the overlay". With no rules, nothing is in scope and
    the run would print a pass having checked nothing.
    """
    rules = load_routing_map(data_root=data_root).get("rules") or {}
    if not rules:
        raise ScopeCollapsed(
            f"{engine_root / 'config' / 'routing-map.yaml'} yielded no rules, "
            f"so no path can be identified as overlay-owned and this scanner "
            f"has nothing it is willing to check. `load_routing_map()` fails "
            f"closed and silently, so an unreadable or unparseable map looks "
            f"exactly like this.")
    return rules


# ============================================================
# Extraction
# ============================================================

def anchor_pattern(rules: dict, overlay_only: frozenset[str]) -> re.Pattern:
    """A regex matching any path whose first segment could be overlay-side.

    DERIVED from the map and from the overlay's own top level, never written
    out, so a family added to either is anchored without anyone remembering
    this function exists. The nine new families reached the pattern through the
    second source on the day they were created, hours before any routing rule
    named them.

    The pattern is deliberately WIDE: it admits `docs/`, `config/`, `scripts/`
    and `reference/`, every one of which is an engine directory too. Narrowing
    it here is the prefix-list mistake the module docstring is about.
    `in_data_scope()` is what decides, per path.
    """
    firsts = sorted(
        {key.split("/")[0] for key, dest in rules.items()
         if dest == "private" and "/" in key}
        | {entry.split("/")[0] for entry in overlay_only},
        key=len, reverse=True,
    )
    if not firsts:
        raise ScopeCollapsed(
            "no routing rule names a directory that routes private, "
            "so there is no data-side path shape to look for")
    alt = "|".join(re.escape(f) for f in firsts)
    # The leading look-behind refuses a match inside a longer token, so
    # `my_outputs/x` and `https://h/outputs/x` do not read as `outputs/x`.  # leak-guard: ok (scanner vocabulary)
    #
    # The tail is OPTIONAL, which it was not until the fixture caught it: a rule
    # saying "principles are filed in `canon/principle/`" is a reference, and so
    # is one saying "everything lands under `outputs/`". Requiring a child
    # segment silently dropped every reference to a family ROOT -- which is the
    # commonest way prose names one of the nine new families, and therefore the
    # commonest reference this scanner exists to catch.
    #
    # The closing class refuses a trailing dot or slash, so "... under
    # `outputs/`." yields `outputs` rather than `outputs/` or `outputs/.`.  # leak-guard: ok (scanner vocabulary)
    #
    # `NOT_A_RESOURCE_NAME` is the fragment the three sibling scanners already
    # place after the root's slash: `people/me` and `people/c<digits>` are  # leak-guard: ok (scanner vocabulary)
    # Google People API resource names, never a data address. This anchor did
    # not carry it, MEASURED 2026-10-13: the contacts connector's 19 resource
    # names counted as 3 unresolved data paths once `people/` became a root.
    return re.compile(
        rf"(?<![\w/.-])((?:{alt})/{NOT_A_RESOURCE_NAME}(?:[A-Za-z0-9_./-]*[A-Za-z0-9_-])?)"
    )


def overlay_prefix_pattern(data_root: Path) -> re.Pattern:
    """Matches the overlay's own directory name used as a path prefix.

    The anchor above refuses a match preceded by `/`, `.`, `-` or a word
    character, which is what keeps `my_outputs/x` and `https://h/outputs/x` from
    reading as references. MEASURED 2026-09-21: that same look-behind makes the
    scanner blind to the one spelling this engine uses when it addresses the
    operator directly. The executive provisioner written with the overlay
    directory on the front yielded NO match, while the same path written
    data-relative yielded one, so three sites in the executive provisioner
    (removed 2026-09-25 with the executive model) naming a tool that had moved
    were invisible to a gate that ran on every push. One of the three is an f-string PRINTED to the operator as a command
    to copy, which is the most expensive place in the tree to be wrong and the
    one place the scanner could not see.

    So the prefix is stripped before the anchor is applied, and the spelling is
    DERIVED from `data_root.name` rather than written out: a name in this file
    would be a second copy of a constant `scripts/utils/paths.py` already owns,
    and it would be wrong on every exec overlay. Leading `../` hops are consumed
    with it, because `scripts/utils/workspace.py` spells the same class of
    reference with a `../` in front of the overlay name.

    The leading look-behind is the anchor's own idea, kept for the same reason:
    without it `my.heading-os-data/outputs/x` has its middle cut out and a
    finding is INVENTED where there is only one long token. `/` is deliberately
    absent from the class, so an absolute path is stripped too and the anchor's
    own look-behind then refuses what is left -- which is the right answer twice
    over.

    What it does NOT cover, stated rather than left to be found: an ABSOLUTE
    path to the overlay is not turned into a finding by this, only neutralised.
    That spelling is forbidden in tracked engine files on its own account, and
    `scripts/leak-guard.py` is what refuses it.
    """
    return re.compile(rf"(?<![\w.-])(?:\.\./)*{re.escape(data_root.name)}/")


# A reference under `tests/` is a SANDBOX path, not a reference into the live
# overlay: a test builds a synthetic data root under `tmp_path` and names
# `auto-memory/x.md` inside it. Resolving those against the operator's real  # leak-guard: ok (scanner vocabulary)
# overlay asks a question nobody posed, and MEASURED 2026-09-12 they are 406 of
# the 521 unresolved paths -- enough to bury every real finding.
#
# They are EXTRACTED, COUNTED, LISTED and written to the artifact, because the
# brief asked for `tests/**` and a silent exclusion is the defect
# `.claude/rules/scope-claims.md` exists to stop. What they do not do is decide
# the exit code, unless `--include-tests` says so. `scripts/leak-guard.py` draws
# the same line in the same place and for the same reason.
FIXTURE_SUBTREE = "tests/"


# ROLES: what a Python literal is FOR, read off the AST node that consumes it.
#
# The role is DERIVED from the consuming node, never from a list of files or
# paths, and it fails closed: anything the derivation does not recognise is
# `path`, which pins. Each recognised role is a shape of Python, stated here:
#
#   docstring   a bare string statement (`ast.Expr`). A sentence for a reader.
#   message     text handed to a human: the argument of `print`, a logging
#               call, an argparse `help=`, or an exception being raised.
#               Reaches a terminal or a log, never the filesystem.
#   boundary    compared AGAINST a path, never joined into one: an operand of
#               `==`/`in`, the FIRST positional argument of `startswith` and its
#               kin, a `re` pattern, or an element of a module-level collection
#               whose every use in that module is such a comparison (one hop of
#               name flow, fail-closed).
#   seam-def    inside a data-root helper's own body in the seam modules. It is
#               the definition of the tree root, and moving records out of the
#               root does not change where the root is.
#   path        everything else. Joined to a Path, opened, globbed, stored in a
#               map that is later joined, or consumed in a way not listed above.
#
# A constant with whitespace INSIDE it is a sentence, but only where the
# consuming node is somewhere text goes: a `return` (through an enclosing tuple,
# one hop at a time), a `raise`, a dict value, an f-string or multi-line
# assignment, a `help=`/`msg=`/`message=`/`detail=`/`reason=` keyword, or a call
# that appends, writes or prints. MEASURED 2026-09-14 over the 1,479 literals
# the syntactic rules left as `path`, 278 carry interior whitespace and every
# one of the 39 live ones is report text or an error string (`"path must be
# under knowledge/"`). No path this engine constructs has a space in it -- but a
# DIRECTORY may, which is why the consumer still has to agree.
#
# A constant carrying a NEWLINE needs no such agreement: it is a document, and
# there is no shape in which a path this engine builds spans lines. That one is
# what carries the `textwrap.dedent("""...""")` templates a provisioning script
# writes into a new workspace, which the consumer rules cannot see because
# `dedent` is a function that returns a string like any other.
#
# THE FOUR SHAPES AN OUTSIDE REVIEW FOUND, 2026-09-14
# ---------------------------------------------------
# Review finding b1 #1, reproduced on this source before it was believed. Four
# non-pinning verdicts, every one of them wrong, and each from a rule that was
# reading evidence it did not have:
#
#   relative.replace("SOURCE", "outputs/operations/council")  -> boundary  # leak-guard: ok (scanner vocabulary)
#   textwrap.dedent("outputs/operations/council")             -> message  # leak-guard: ok (scanner vocabulary)
#   (data_root / "outputs/client reports").glob("*.md")       -> message  # leak-guard: ok (scanner vocabulary)
#   re.sub("x", "outputs/intel", s)                           -> boundary  # leak-guard: ok (scanner vocabulary)
#
# Three repairs for the four shapes, and TWO of the three narrow a rule. A
# compare method counts only its FIRST positional argument, because the second
# argument of `replace` and of `re.sub` is the string being CONSTRUCTED; that
# one gate answers two of the four shapes. And `/` left the set of nodes the
# consumer search walks THROUGH, so a literal under a `pathlib` join is decided
# by the join and not by whatever sits above it, asked first, before every rule
# but the seam's own definition.
#
# The third is NOT a narrowing, and saying it was is how this comment read until
# 2026-09-14. `dedent`, `fill` and `wrap` left `_MESSAGE_SINKS` because they
# return a string rather than consuming one, but that sentence is the reasoning,
# not the mechanism: nothing here decides whether a callee returns or consumes,
# so the repair is three names deleted from a hardcoded set and any other
# string-returning wrapper on it would reproduce the defect unseen. What carries
# the shape generally is a different rule, and it is a BROADENING rather than a
# narrowing: a literal containing a newline is text whatever consumes it, which
# is what holds the `textwrap.dedent("""...""")` templates now. The two are
# recorded apart because only the first is the kind of fix that generalises.
# All four, plus the review's counter-input `root / "outputs/customer notes/a.md"`,  # leak-guard: ok (scanner vocabulary)
# were held by one run until the 2026-09-21 test-suite split parked it. The four
# rules are still below and nothing exercises them.
#
# `prose` is the role of every non-Python site; those were never pins.
#
# THE ORDER THE RULES ARE ASKED IN, and it is part of the specification:
#
#   1. a bare string statement is a docstring, whatever module it sits in;
#   2. a seam helper's own body, in the two seam modules, is `seam-def`;
#   3. a literal under a `pathlib` join is a path SEGMENT, and nothing above
#      the join is consulted. This is NOT the b1 #1 repair, which is `/`
#      being absent from `_STRING_OPS` (step 4 and everything after it look
#      upward, and `consumer()` now stops AT the join). What this step
#      decides on its own is step 6, the only rule below that answers for a
#      `BinOp` without a consumer agreeing: `root / "a\nb"` read as a
#      document until 2026-09-14. Asked here, it answers `path`, which is
#      the fail-closed direction;
#   4. compared, not joined -> `boundary`, first positional argument only;
#   5. a `help=`/`msg=`/... keyword, or a print/log callee -> `message`,
#      with no whitespace required: `help="outputs/"` is one token and still
#      a line of text for a human;
#   6. a constant spanning LINES is a document whatever consumes it;
#   7. interior whitespace AND a consumer where text goes -> `message`;
#   8. an element of a module-level collection every use of which is a
#      comparison -> `boundary`, one hop, fail-closed;
#   9. everything else -> `path`, which pins.

#: The status an address earns when its own vocabulary does not name it.
STATUS_UNKNOWN_ADDRESS = "unknown-address"

ROLE_PROSE = "prose"
ROLE_DOCSTRING = "docstring"
ROLE_MESSAGE = "message"
ROLE_BOUNDARY = "boundary"
ROLE_SEAM = "seam-def"
ROLE_PATH = "path"
ROLES: tuple[str, ...] = (ROLE_PROSE, ROLE_DOCSTRING, ROLE_MESSAGE,
                          ROLE_BOUNDARY, ROLE_SEAM, ROLE_PATH)

#: The two modules whose `get_*_dir()` bodies DEFINE the tree roots. The same
#: pair `helper_names()` reads; named once so the two cannot drift.
SEAM_MODULES: tuple[str, ...] = ("scripts/utils/workspace.py", "scripts/utils/paths.py")

#: The HOS-5 front door. Its operations resolve a data address exactly as the
#: seam helpers do, so a file calling one RESOLVES rather than spells, and the
#: "files resolving through helpers" gauge has to see it.
#:
#: It is deliberately NOT added to `SEAM_MODULES`, and the distinction is the
#: whole point of the separate constant. `SEAM_MODULES` does a second job at
#: `_RoleFinder.inside_seam_helper`: a path literal inside a helper body in one
#: of those two files is classified `seam-def` and stops counting as a path.
#: Extending that exemption to `scripts/utils/data_path.py` would stop checking
#: the one module the gate most needs to check, which is the same trade
#: `SEAM_ALLOWLIST` refused on 2026-09-19. This constant grants recognition and
#: no exemption.
#:
#: MEASURED 2026-09-21 in the yard hos5-phase3-consumers, before and after
#: converting 63 seam call sites in 42 files onto `data_path()`: with the gauge
#: blind to the front door it fell from 320 files to 294, so a change that put
#: 26 more files ONTO a resolver read as 26 files coming off one.
FRONT_DOOR_MODULE = "scripts/utils/data_path.py"

#: Terminal callee names whose string argument is text for a human. `dedent`,
#: `fill` and `wrap` were here until 2026-09-14 and are not sinks at all: they
#: RETURN a string, so `textwrap.dedent("outputs/operations/council")` reaches  # leak-guard: ok (scanner vocabulary)
#: whatever the caller does with the result, which may well be a join.
_MESSAGE_SINKS: frozenset[str] = frozenset({
    "print", "log", "_log", "debug", "info", "warning", "warn", "error",
    "critical", "exception", "fatal", "notify",
})
#: Callee names that take a line of TEXT and put it somewhere textual. Only
#: consulted for a literal carrying interior whitespace; see `_SENTENCE_KEYWORDS`
#: and `role()`.
_TEXT_SINKS: frozenset[str] = frozenset({
    "append", "extend", "insert", "write", "writelines", "join",
}) | _MESSAGE_SINKS
#: Keyword arguments whose value is a line of text for a human.
_SENTENCE_KEYWORDS: frozenset[str] = frozenset({
    "help", "msg", "message", "detail", "reason",
})
#: `str`/`re` methods whose FIRST POSITIONAL ARGUMENT is matched against the
#: receiver. Only `args[0]` counts: `replace(old, NEW)` and `re.sub(pat, REPL,
#: s)` CONSTRUCT the string in their second argument, so a data path there is a
#: path under construction and reading it as a comparison unpinned it silently.
_COMPARE_METHODS: frozenset[str] = frozenset({
    "startswith", "endswith", "removeprefix", "removesuffix", "replace",
    "split", "rsplit", "partition", "rpartition", "lstrip", "rstrip", "strip",
    "find", "rfind", "index", "rindex", "count", "match", "search", "fullmatch",
    "compile", "escape", "sub", "subn", "findall", "finditer",
})
#: Binary operators that assemble a STRING. `/` and `//` are absent on purpose:
#: they are `pathlib` joining a Path, so a literal under one is a path SEGMENT
#: and the nodes above the join say nothing about it. Walking through `Div` is
#: how `(data_root / "outputs/client reports").glob(...)` read as a message.  # leak-guard: ok (scanner vocabulary)
_STRING_OPS = (ast.Add, ast.Mod)


def _is_string_builder(node: ast.AST) -> bool:
    """True for a node that only ASSEMBLES a string; the consumer is above it."""
    if isinstance(node, (ast.JoinedStr, ast.FormattedValue)):
        return True
    return isinstance(node, ast.BinOp) and isinstance(node.op, _STRING_OPS)


def _is_path_join(node: ast.AST | None) -> bool:
    """True when `node` is a `pathlib` join: `a / b` or `a // b`."""
    return isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Div, ast.FloorDiv))


class Reference(NamedTuple):
    """One data-relative path, and the engine site that names it."""

    path: str        # data-relative: an overlay directory and what is under it
    file: str        # engine-relative: the skill, script or document naming it
    line: int
    kind: str        # "python-literal" | "prose"
    role: str = ROLE_PATH   # one of ROLES; see the block above
    # The line carries `INTENTIONAL_MARKER`. Such a site is left out of every
    # count here EXCEPT the HOS-5 section 6 prohibition; see `PinFinding`.
    marked: bool = False
    # First and last source line of the statement holding the literal, for a
    # Python literal; (0, 0) otherwise. See `_statement_span`.
    span: tuple[int, int] = (0, 0)
    # Assembled from SEGMENTS (`root / "crm" / "contacts"`, `os.path.join`,
    # `joinpath`, `f"{root}/crm/..."`), none of which spells the address alone.
    # Read by the section 6 prohibition only; see `_assembled_references`.
    assembled: bool = False

    @property
    def is_address(self) -> bool:
        """A HOS-5 logical NAME rather than a directory under the data root.

        The two are resolved by different machinery and must not be told apart
        by a second field that can disagree with the string: the string is the
        evidence.
        """
        return self.path.startswith((RECORD_SCHEME, RUNTIME_SCHEME))

    @property
    def is_fixture(self) -> bool:
        # A literal under `tests/` names a tree the test itself built under
        # `tmp_path`; see FIXTURE_SUBTREE. It follows the code it tests, so it
        # cannot hold a record in the overlay where it is, whatever its role.
        return self.file.startswith(FIXTURE_SUBTREE)

    @property
    def pins(self) -> bool:
        """Would moving the records under `path` break this site?

        Only a live literal that constructs a path. A fixture under `tests/`
        builds a sandbox tree and follows the code it tests; every other role
        is a sentence or a comparison. Stated here once.
        """
        return self.role == ROLE_PATH and not self.is_fixture and not self.is_address


def resolve_address(rel: str) -> tuple[str, str]:
    """(`ok` | `unknown-address`, why) for a HOS-5 scheme-qualified name.

    A thin dressing of `data_path.validate_address` into this file's verdict
    pair. The vocabulary question is asked THERE, once, by all three guards that
    ask it; a second copy here is the shape both this scanner and that module
    exist to remove. What it does and deliberately does not check -- engine
    constants only, never `known_names()`, nothing about a bare registry name --
    is written out at that function, not restated here where it would drift.
    """
    why = validate_address(rel)
    if why is None:
        return "ok", f"the engine's own vocabulary places {rel}"
    return STATUS_UNKNOWN_ADDRESS, why


def _is_sentence(value: str) -> bool:
    """Interior whitespace: words, not a path. See the ROLES block."""
    return any(ch.isspace() for ch in value.strip())


def _callee_name(call: ast.Call) -> str:
    fn = call.func
    if isinstance(fn, ast.Attribute):
        return fn.attr
    if isinstance(fn, ast.Name):
        return fn.id
    return ""


def _is_re_call(call: ast.Call) -> bool:
    fn = call.func
    return (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)
            and fn.value.id == "re")


class _RoleFinder:
    """Assigns a role to each string constant of one parsed module.

    Built once per file: a parent map, the module-level single assignments, and
    every `Load` of a module-level name, so the one-hop name flow for
    `boundary` is a lookup rather than a second walk per literal.
    """

    def __init__(self, tree: ast.Module, rel: str, helpers: set[str]):
        self.rel = rel
        self.helpers = helpers
        self.parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                self.parents[child] = node
        # Module-level `NAME = <collection>`, assigned exactly once.
        assigned: dict[str, int] = {}
        for stmt in tree.body:
            targets = []
            if isinstance(stmt, ast.Assign):
                targets = stmt.targets
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                targets = [stmt.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    assigned[t.id] = assigned.get(t.id, 0) + 1
        self.single_assigned = {n for n, c in assigned.items() if c == 1}
        self.loads: dict[str, list[ast.Name]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                self.loads.setdefault(node.id, []).append(node)

    # -- consumers --------------------------------------------------------

    def consumer(self, node: ast.AST) -> ast.AST | None:
        """The nearest ancestor that is not merely assembling a string."""
        p = self.parents.get(node)
        while _is_string_builder(p):
            p = self.parents.get(p)
        return p

    def enclosing_function(self, node: ast.AST):
        p = self.parents.get(node)
        while p is not None:
            if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return p
            p = self.parents.get(p)
        return None

    def _compared(self, node: ast.AST, p: ast.AST | None) -> bool:
        """Is `node` an operand of a comparison or a str-comparison method?

        For a call, ONLY the first positional argument counts. That is the
        argument matched against the receiver in every method on the list
        (`startswith(x)`, `replace(OLD, new)`, `re.sub(PAT, repl, s)`); the rest
        are replacements, which CONSTRUCT a string. Until 2026-09-14 any
        argument position satisfied this, so the constructed half of a
        `replace()` or a `re.sub()` was recorded as a comparison and unpinned
        the records it names.
        """
        if isinstance(p, ast.Compare):
            return True
        if isinstance(p, ast.Call):
            first = p.args[0] if p.args else None
            if first is None or not (first is node or self._reaches(first, node)):
                return False
            if _is_re_call(p):
                return True
            fn = p.func
            if isinstance(fn, ast.Attribute) and fn.attr in _COMPARE_METHODS:
                return True
        return False

    def _reaches(self, top: ast.AST, node: ast.AST) -> bool:
        """Is `node` inside `top` through string-builder nodes only?"""
        p = node
        while p is not None and p is not top:
            p = self.parents.get(p)
            if p is top:
                return True
            if not _is_string_builder(p):
                return False
        return p is top

    def _name_only_compared(self, name: str, depth: int) -> bool:
        """Every Load of a module-level `name` feeds a comparison (one hop)."""
        if depth > 2 or name not in self.single_assigned:
            return False
        uses = self.loads.get(name, [])
        if not uses:
            return False
        for use in uses:
            p = self.parents.get(use)
            # `x in NAME`, `x not in NAME`
            if isinstance(p, ast.Compare) and use in p.comparators:
                continue
            # `for t in NAME:` / `(... for t in NAME)` / `NAME.items()` etc.
            iter_owner = p
            if (isinstance(p, ast.Attribute) and p.attr in ("items", "keys", "values")
                    and isinstance(self.parents.get(p), ast.Call)):
                iter_owner = self.parents.get(self.parents.get(p))
            if (isinstance(iter_owner, (ast.For, ast.comprehension))
                    and (iter_owner.iter is use
                         or iter_owner.iter is self.parents.get(self.parents.get(use)))):
                targets = ([iter_owner.target] if isinstance(iter_owner.target, ast.Name)
                           else list(getattr(iter_owner.target, "elts", [])))
                if not targets or not all(isinstance(t, ast.Name) for t in targets):
                    return False
                if not all(self._local_only_compared(t.id, iter_owner) for t in targets):
                    return False
                continue
            return False
        return True

    def _local_only_compared(self, name: str, scope: ast.AST) -> bool:
        """Every Load of loop variable `name` under `scope` is a comparison."""
        fn = self.enclosing_function(scope) or scope
        uses = [n for n in ast.walk(fn)
                if isinstance(n, ast.Name) and n.id == name
                and isinstance(n.ctx, ast.Load)]
        if not uses:
            return False
        return all(self._compared(u, self.consumer(u)) for u in uses)

    # -- the role -----------------------------------------------------------

    def _sentence_consumer(self, node: ast.Constant, p: ast.AST | None) -> bool:
        """May a literal carrying interior whitespace be read as TEXT here?

        The whitespace rule is evidence about the literal, never about what is
        done with it, so it decides nothing on its own: a constant with a space
        in it is text only where the consuming node is somewhere text goes.
        Everything else -- an unknown call, a `Path(...)`, a `glob`, a
        subscript, an attribute, a `/` join -- keeps the fail-closed `path`,
        because a directory joined under the data root may well have a space in
        its own name, and no rule about words can see the difference.
        """
        if isinstance(p, (ast.Return, ast.Raise)):
            return True
        if isinstance(p, (ast.Tuple, ast.List, ast.Set)):
            # A tuple in a `return` is a return. One hop up, recursively, so
            # `return "person", "matched crm/contacts/"` is the sentence it  # leak-guard: ok (scanner vocabulary)
            # reads as, while a module-level list of path fragments still
            # lands on its `Assign` and stays `path`.
            return self._sentence_consumer(node, self.parents.get(p))
        if isinstance(p, ast.Dict):
            return any(v is node or self._reaches(v, node) for v in p.values)
        if isinstance(p, ast.Assign):
            # No name-flow is attempted: a plain `x = "..."` says nothing about
            # what `x` becomes. Only the two shapes that are text by
            # construction count -- an f-string, or a constant spanning lines.
            return isinstance(p.value, ast.JoinedStr) or "\n" in node.value
        if isinstance(p, ast.Call):
            if _callee_name(p) in _TEXT_SINKS:
                return True
            # `raise SystemExit("nothing under knowledge/ yet")`: the exception
            # being constructed IS the message, and the constructor's name is
            # whatever the module chose to call it, so the callee list cannot
            # answer for it. The `Raise` above it can.
            return isinstance(self.parents.get(p), ast.Raise)
        if isinstance(p, ast.keyword):
            return p.arg in _SENTENCE_KEYWORDS
        return False

    def _seam_definition(self, node: ast.Constant) -> bool:
        """Inside a data-root helper's own body, in one of the seam modules.

        Both halves are required: the same source under `scripts/other.py` is a
        path, because a helper NAME anywhere else is a function that happens to
        share a name with the seam. Each half had its own case until the
        2026-09-21 test-suite split parked them, so dropping either check below
        now widens the exemption in silence.
        """
        if self.rel not in SEAM_MODULES:
            return False
        fn = self.enclosing_function(node)
        return fn is not None and fn.name in self.helpers

    def _collection_boundary(self, node: ast.AST, p: ast.AST | None) -> bool:
        """An element of a module-level collection used only for comparison.

        One hop of name flow and no further, and it fails closed: a single use
        of the container outside a comparison, `root / TOKENS[0]`, puts every
        element back to `path`.
        """
        if not isinstance(p, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
            return False
        owner = self.parents.get(p)
        if not (isinstance(owner, ast.Assign) and len(owner.targets) == 1
                and isinstance(owner.targets[0], ast.Name)):
            return False
        return self._name_only_compared(owner.targets[0].id, 1)

    def role(self, node: ast.Constant) -> str:
        """The role of one string constant, from the node that CONSUMES it.

        The order is the specification; it is written out in the ROLES block
        above and each step has its failing half in the test file named there.
        Anything this does not recognise is `path`, which pins.
        """
        if isinstance(self.parents.get(node), ast.Expr) and self.parents[node].value is node:
            return ROLE_DOCSTRING
        if self._seam_definition(node):
            return ROLE_SEAM
        p = self.consumer(node)
        # A `pathlib` join decides on its own, and is asked before the rules
        # that look upward: `(data_root / "outputs/client reports").glob(...)`  # leak-guard: ok (scanner vocabulary)
        # reaches a `glob` and a whitespace literal on the way past.
        #
        # Stated rather than assumed, and narrowed twice. What holds the b1 #1
        # repair is `/` being absent from `_STRING_OPS`, so `consumer()` stops
        # AT the join and every rule that looks UPWARD falls through to `path`.
        # These two lines are not that repair, and until 2026-09-14 no test
        # observed them refusing: MEASURED that day, deleting them left the
        # test file green, because no rule below had anything to say about a
        # `BinOp`. One does. A constant spanning LINES is a document whatever
        # consumes it, asked two steps down and with no consumer required to
        # agree, so `root / "outputs/intel\nsecond line"` read as `message` and  # leak-guard: ok (scanner vocabulary)
        # unpinned every record under it. This step forces the fail-closed
        # answer there -- a literal joined to a Path is a path SEGMENT, and a
        # newline in it is a defect in the scanned code, not evidence about the
        # role. One case used to fail without these two lines, the one where a
        # path join has to beat the newline rule; the 2026-09-21 test-suite
        # split parked it, so deleting them is green again, exactly as it was
        # before 2026-09-14.
        if _is_path_join(p):
            return ROLE_PATH
        if self._compared(node, p):
            return ROLE_BOUNDARY
        if isinstance(p, ast.keyword) and p.arg in _SENTENCE_KEYWORDS:
            return ROLE_MESSAGE
        if isinstance(p, ast.Call) and _callee_name(p) in _MESSAGE_SINKS:
            return ROLE_MESSAGE
        # No path this engine builds spans lines, so a newline needs no
        # consumer to agree with it. This is what carries the
        # `textwrap.dedent("""...""")` templates now that `dedent` is not a sink.
        if "\n" in node.value:
            return ROLE_MESSAGE
        if _is_sentence(node.value) and self._sentence_consumer(node, p):
            return ROLE_MESSAGE
        if self._collection_boundary(node, p):
            return ROLE_BOUNDARY
        return ROLE_PATH


def extract_python(text: str, rel: str, pattern: re.Pattern,
                   helpers: set[str] | None = None,
                   overlay_prefix: re.Pattern | None = None) -> list[Reference]:
    """Data paths named by `str` constants in a Python file, each with its role.

    The AST, not the text. A substring scan over source goes red the moment a
    fix quotes the pattern it removed in order to explain it, which teaches
    people to stop explaining; and it cannot tell a literal from a comment.
    `ast.walk` reaches f-string parts (`JoinedStr` holds `Constant` children)
    and docstrings, both of which name real paths in this tree.

    `helpers` is the set `helper_names()` derives; it decides `seam-def` and
    nothing else. Absent, no literal can be `seam-def`, which fails closed.

    `overlay_prefix` strips the overlay's own directory name off the front of a
    literal before the anchor is applied; see `overlay_prefix_pattern()` for the
    measurement. Absent, nothing is stripped, which is the previous behaviour.

    Raises SyntaxError to the caller. A file that does not parse has not been
    checked, and swallowing that here would shrink the corpus in silence.
    """
    tree = ast.parse(text)
    lines = text.splitlines()
    finder = _RoleFinder(tree, rel, helpers or set())
    out: list[Reference] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        lineno = getattr(node, "lineno", 0)
        source_line = lines[lineno - 1] if 0 < lineno <= len(lines) else ""
        # Returned FLAGGED rather than dropped: `scan()` sets marked sites
        # aside from every count except the section 6 prohibition, which the
        # marker does not answer. See `PinFinding`.
        marked = INTENTIONAL_MARKER in source_line
        addresses, masked = addresses_in(node.value)
        if overlay_prefix is not None:
            masked = overlay_prefix.sub("", masked)
        hits = list(pattern.finditer(masked))
        if not hits and not addresses:
            continue
        role = finder.role(node)
        span = _statement_span(finder, node)
        for address in addresses:
            out.append(Reference(address, rel, lineno, "python-literal", role,
                                 marked, span))
        for match in hits:
            hit = match.group(1).rstrip("/")
            out.append(Reference(hit, rel, lineno, "python-literal", role,
                                 marked or _marked_inside(node, lines, masked, match),
                                 span))
    out.extend(_assembled_references(tree, finder, lines, rel, pattern, overlay_prefix))
    return out


def _marked_inside(node: ast.Constant, lines: list[str], text: str,
                   match: re.Match) -> bool:
    """The marker on the source line a hit INSIDE a multi-line literal sits on.

    `marked` above reads the literal's FIRST line, which is the whole answer for
    a one-line string and the wrong one for a docstring: a `leak-guard: ok` on
    the line that names the path, the place this tree's authors put it, was
    never read. MEASURED 2026-10-13 after the C3 move: of the 44 unresolved
    paths, 2 were named ONLY on such marked docstring lines and counted anyway,
    and one legacy root had 18 of its 20 sites marked that way.

    The hit's line is found by OCCURRENCE, not by counting newlines: the hit is
    the k-th appearance of its text in the literal, so it sits on the source
    line holding the k-th appearance among the literal's own lines. A newline
    count drifts on a `\\` continuation or a written `\\n` escape; a path holds
    neither, so its occurrences in the source are the ones in the value, both
    counted with scheme addresses blanked as `addresses_in` blanks the value.
    Where the source lines do not hold k+1 of them, the first-line reading
    stands.
    """
    end = getattr(node, "end_lineno", None) or node.lineno
    if end == node.lineno:
        return False
    needle = match.group(1)
    k = text.count(needle, 0, match.start())
    for at in range(node.lineno, min(end, len(lines)) + 1):
        line = lines[at - 1]
        here = addresses_in(line)[1].count(needle)
        if k < here:
            return INTENTIONAL_MARKER in line
        k -= here
    return False


#: Callees whose positional arguments are path SEGMENTS joined in order.
_SEGMENT_JOINERS = frozenset({"joinpath", "Path", "PurePath", "PurePosixPath"})


def _segments(node: ast.AST) -> list[ast.AST]:
    """The operands of a `/` chain, left to right."""
    if _is_path_join(node):
        return _segments(node.left) + _segments(node.right)
    return [node]


def _assembled_references(tree: ast.Module, finder: "_RoleFinder", lines: list[str],
                          rel: str, pattern: re.Pattern,
                          overlay_prefix: re.Pattern | None) -> list[Reference]:
    """Data addresses no single literal spells: the segments of one join, read together.

    MEASURED 2026-09-25 by an outside review of this gate: `root / "crm" /
    "contacts"`, `os.path.join(root, "crm", "contacts")` and `f"{root}/crm/..."`
    each reached no count, while the prohibition printed "0 unexplained", with
    a live example in the tree. Adjacent string constants of one `/` chain, one
    `os.path.join`, one `joinpath`/`Path(...)` call are joined with `/` and read
    by the same anchor; an f-string constant that opens with `/` after a
    placeholder is read without it. Returned `assembled`, so that like a marked
    literal it reaches the section 6 prohibition and no other count here.
    """
    # (text, the constant that anchors the site, its role)
    found: list[tuple[str, ast.Constant, str]] = []

    def joined_runs(parts: list[ast.AST]) -> None:
        run: list[ast.Constant] = []
        for part in parts + [None]:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                run.append(part)
                continue
            if len(run) >= 2:
                role = ROLE_SEAM if finder._seam_definition(run[0]) else ROLE_PATH
                found.append(("/".join(c.value.strip("/") for c in run), run[0], role))
            run = []

    for n in ast.walk(tree):
        if _is_path_join(n) and not _is_path_join(finder.parents.get(n)):
            joined_runs(_segments(n))
        elif isinstance(n, ast.Call) and (
                _callee_name(n) in _SEGMENT_JOINERS
                or (_callee_name(n) == "join" and isinstance(n.func, ast.Attribute)
                    and isinstance(n.func.value, ast.Attribute)
                    and n.func.value.attr == "path")):
            joined_runs(list(n.args))
        elif isinstance(n, ast.JoinedStr):
            for prev, part in zip(n.values, n.values[1:], strict=False):
                if (isinstance(prev, ast.FormattedValue) and isinstance(part, ast.Constant)
                        and isinstance(part.value, str) and part.value.startswith("/")):
                    found.append((part.value.lstrip("/"), part, finder.role(part)))
    out: list[Reference] = []
    for text, anchor, role in found:
        if overlay_prefix is not None:
            text = overlay_prefix.sub("", text)
        line = lines[anchor.lineno - 1] if 0 < anchor.lineno <= len(lines) else ""
        span = _statement_span(finder, anchor)
        for match in pattern.finditer(text):
            out.append(Reference(match.group(1).rstrip("/"), rel, anchor.lineno,
                                 "python-literal", role,
                                 INTENTIONAL_MARKER in line, span, True))
    return out


def _statement_span(finder: "_RoleFinder", node: ast.AST) -> tuple[int, int]:
    """First and last line of the statement a literal belongs to.

    The innermost enclosing statement, found by walking up the parent map, so a
    literal in a function body belongs to its own line's statement and never to
    the function. A compound statement (`if`, `for`, `with`, `def`, an `except`
    clause) contributes its HEADER only: its body holds other statements, judged
    on their own, which is the rule `scripts/dev/import-env-purity.py` keys on.
    """
    lit_start = getattr(node, "lineno", 0)
    lit_end = getattr(node, "end_lineno", None) or lit_start
    anc = finder.parents.get(node)
    while anc is not None and not isinstance(
            anc, (ast.stmt, ast.ExceptHandler, ast.match_case)):
        anc = finder.parents.get(anc)
    if anc is None:
        return lit_start, lit_end
    start = min(getattr(anc, "lineno", lit_start), lit_start)
    body = getattr(anc, "body", None)
    if isinstance(body, list) and body and isinstance(body[0], ast.stmt):
        end = max(body[0].lineno - 1, lit_end)
    else:
        end = getattr(anc, "end_lineno", None) or lit_end
    return start, end


# A rule DECLARATION in the routing map: `  "outputs/": private`. The key is a
# LABEL for a class of paths, not a reference to a file, and this scanner takes
# its own scope from those keys -- so reading them back as references is the tool
# asking itself a question and then reporting its own answer as a finding. On
# 2026-09-12 that produced `crm/contacts` and `docs/design` as "missing" in any  # leak-guard: ok (history: measured old path)
# tree where the overlay happened not to hold them.
#
# Only the DECLARATION line is skipped. The map's comments are ordinary prose and
# stay in: a comment naming `_archive/books` is a real reference to a real path,
# and dropping the whole file (the `CHANGELOG.md` treatment the sibling scanner
# uses) would have lost it.
_RULE_DECLARATION = re.compile(r'^\s*["\'][^"\']+["\']\s*:')


# The engine's default `names:` block. Its `under:` values are read out of the
# prose scan STRUCTURALLY, never by a pattern on the line; see
# `mask_registry_history()`.
REGISTRY_TEMPLATE_REL = "/".join(EXAMPLE_NAMES_REL)


#: The `names.<entry>` fields of the TEMPLATE whose values are blanked. `under`
#: since HOS-127 (2026-09-25), for the reason in the docstring below. `path`
#: since HOS-240 (2026-10-13): the template stopped mirroring the operator's
#: layout that day. Every entry is now a `path:` into the generic C3 subject
#: tree a FRESH data repository is scaffolded with (`scripts/init-data.py`),
#: and the template is read only by a tree whose registry has no `names:`
#: block, which this overlay never is. So a template `path:` value is not a
#: reference into this overlay, exactly as an `under:` value was not: MEASURED
#: 2026-10-13, 18 of its 166 values named subject addresses this overlay has
#: not created yet (its unmoved files still sit at legacy addresses its OWN
#: registry names) and would have counted as 18 unresolved data paths. Nothing
#: else in the file, and nothing in any other file, is blanked.
_TEMPLATE_MASKED_FIELDS = frozenset({"under", "path"})


def mask_registry_history(text: str) -> str:
    """`text` with the value of every `names.<entry>.under` blanked out.

    WHY. HOS-127, MEASURED 2026-09-25: 51 of the 147 entries in `new` were
    addresses that only `scripts/data-names.example.yaml` named, and each was
    the value of an `under:` key. That field is never a live, root-relative
    address. For a `record:` entry it is the PRE-MOVE spelling by the
    template's own definition, the place the record lay before `<legacy>`
    became `<family>/<leaf>/<legacy>`; for a `path:` or `runtime:` entry it is
    relative to that base, not to the data root. Counting it reported history
    as debt and left the ratchet one entry from refusing a push over no defect.
    With the values blanked the count on this tree was 96, the same set as a
    probe with every `under:` value replaced by a placeholder.

    HOW, and why not a pattern. The document is COMPOSED and only the scalar
    nodes at `names` -> <entry mapping> -> `under` are blanked, by their exact
    character span, with newlines kept so every other line number holds. A
    `what:` sentence, a `path:` or `record:` base, a comment, or an `under:`
    anywhere else in the file is read exactly as before. A text rule on
    `under:` lines would also have swallowed a comment or a second address on
    the same line, and would keep matching if the key moved out of `names:`.

    Raises `yaml.YAMLError` on a file that does not compose; the caller decides
    what that means (K008: the handler lives at the call site).
    """
    root = yaml.compose(text, Loader=yaml.SafeLoader)
    if not isinstance(root, yaml.MappingNode):
        return text
    spans: list[tuple[int, int]] = []
    for key, block in root.value:
        if key.value != "names" or not isinstance(block, yaml.MappingNode):
            continue
        for _name, entry in block.value:
            if not isinstance(entry, yaml.MappingNode):
                continue
            for field, value in entry.value:
                if field.value in _TEMPLATE_MASKED_FIELDS and isinstance(value, yaml.ScalarNode):
                    spans.append((value.start_mark.index, value.end_mark.index))
    chars = list(text)
    for start, end in spans:
        for i in range(start, end):
            if chars[i] != "\n":
                chars[i] = " "
    return "".join(chars)


def extract_prose(text: str, rel: str, pattern: re.Pattern,
                  *, is_routing_map: bool = False,
                  overlay_prefix: re.Pattern | None = None) -> list[Reference]:
    """Data paths named anywhere in a non-Python file, line by line.

    `overlay_prefix` is the same stripper `extract_python` applies, for the same
    reason: prose addresses the operator, and prose is where the overlay gets
    spelled out by name. See `overlay_prefix_pattern()`.
    """
    out: list[Reference] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if INTENTIONAL_MARKER in line:
            continue
        if is_routing_map and _RULE_DECLARATION.match(line):
            continue
        addresses, masked = addresses_in(line)
        if overlay_prefix is not None:
            masked = overlay_prefix.sub("", masked)
        for address in addresses:
            out.append(Reference(address, rel, lineno, "prose", ROLE_PROSE))
        hits: list[str] = []
        for variant in expand_prefix_groups(masked):
            for hit in pattern.findall(variant):
                if hit not in hits:
                    hits.append(hit)
        for hit in hits:
            out.append(Reference(hit.rstrip("/"), rel, lineno, "prose", ROLE_PROSE))
    return out


#: A brace group whose every option is empty or a run of directory segments
#: ending in `/`, standing directly before a path: `{status/matter/}threads/x`,  # leak-guard: ok (scanner vocabulary)
#: `{,canon/reference/}context/*.md`. It is the PREFIX GROUP the memory-index  # leak-guard: ok (scanner vocabulary)
#: globs are written in (`config/memory-index.yaml`,
#: `scripts/dev/derive-memory-index-globs.py`).
_PREFIX_GROUP_RE = re.compile(r"\{((?:[A-Za-z0-9_.-]+/)*(?:,(?:[A-Za-z0-9_.-]+/)*)*)\}(?=[A-Za-z0-9_])")

#: Expansions of one line past which the line is read as written. A prose line
#: with more prefix groups than this is not a glob anyone wrote by hand.
_PREFIX_GROUP_CAP = 64


def expand_prefix_groups(line: str) -> list[str]:
    """`line` with every prefix group replaced by each of its options in turn.

    Added 2026-09-25 (HOS-5 section 7). The anchor above refuses a match
    preceded by `/`, `.`, `-` or a word character, and `}` is none of them, so
    `glob: "{status/matter/}threads/business/*.md"` was read as the bare (leak-guard: ok, history)
    `threads/business`, which the move emptied, while the address the glob (leak-guard: ok, history)
    actually matches is `status/matter/threads/business`, which is where the (leak-guard: ok, history)
    records are. MEASURED that day: the eight live layer globs of
    `config/memory-index.yaml` were reported as eight dead addresses, every one
    of them the prefix-less tail of a glob that resolves.

    So each group is expanded rather than stripped: every option is checked,
    the empty option included, which is the bare pre-move spelling the glob
    really does still match when the group carries one. Nothing is exempted. A
    group whose options are not all prefixes (`{intel,deliverables}`) is not a
    prefix group and is left alone, exactly as before.
    """
    variants = [line]
    while True:
        grown: list[str] = []
        changed = False
        for text in variants:
            match = _PREFIX_GROUP_RE.search(text)
            if match is None:
                grown.append(text)
                continue
            changed = True
            for option in match.group(1).split(","):
                grown.append(text[:match.start()] + option + text[match.end():])
        if not changed or len(grown) > _PREFIX_GROUP_CAP:
            return variants if changed else grown
        variants = grown


#: Where the overlay lists the addresses clause 2 cannot see by name, joined
#: under its `config/` directly: a filename, never a data address, as the
#: routing map's own private half was until HOS-232 retired it.
LEGACY_ADDRESSES_FILENAME = "legacy-data-addresses.txt"


def legacy_data_addresses(data_root: Path) -> frozenset[str]:
    """Data addresses engine text may name that no overlay top-level name shows.

    Legacy addresses from before the HEADING OS 2 move, and subtrees under the
    names both trees use (`config/`, `docs/`, `reference/`). One per line, `#`
    for comments; a trailing `/` marks a directory, as in the routing map.
    Until HOS-232 they were the rules of the private half of the routing map,
    every one `private`; nothing reads that half now, and these are its keys
    that clause 2 cannot reach.

    ABSENT IS A REFUSAL. MEASURED 2026-10-12 with the private half gone and no
    list: 200 missing references fell to 46 and 23 new unresolved to 9, which
    is the push ratchet passing over a scope that shrank by itself.
    """
    path = data_root / "config" / LEGACY_ADDRESSES_FILENAME
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ScopeCollapsed(
            f"the overlay's list of legacy data addresses {path} could not be "
            f"read ({type(exc).__name__}); without it this scanner cannot see "
            f"references into pre-migration addresses and would report a "
            f"smaller pass (HOS-232)") from exc
    entries = frozenset(line.strip() for line in text.splitlines()
                        if line.strip() and not line.lstrip().startswith("#"))
    if not entries:
        raise ScopeCollapsed(f"{path} names no address, so it cannot be the "
                             f"list this scanner needs")
    return entries


def overlay_only_segments(engine_root: Path, data_root: Path) -> frozenset[str]:
    """Top-level names the OVERLAY has and this checkout does not.

    The second half of the scope decision, and the half a routing rule cannot
    make. `config/routing-map.yaml` had no rule for any of the nine new families
    on 2026-09-12, so every reference into `canon/`, `ledger/`, `trace/` and
    their siblings resolved through `default: engine` and was invisible to a
    scope built on rules alone -- a blind spot exactly over the tree this
    scanner exists to watch. Deriving the set from the two trees closes it
    without waiting for the map to catch up, and keeps catching the next family
    somebody adds.

    It also draws the `docs/` line the brief named, from measurement rather than
    from a list. `admin`, `config`, `docs`, `reference`, `scripts` and `tests`
    are top-level in BOTH trees, so they are NOT overlay-only and an explicit
    rule is the only thing that can bring one of them in: `docs/design/` is
    checked and `docs/DEPLOYMENT.md` is not.
    """
    engine_names = {p.name for p in engine_root.iterdir()}
    return frozenset(
        p.name for p in data_root.iterdir()
        if p.is_dir() and not p.name.startswith(".") and p.name not in engine_names
    )


def in_data_scope(path: str, overlay_only: frozenset[str],
                  data_root: Path) -> bool:
    """True when this scanner may resolve `path` against the overlay.

    `data_root` is the overlay the caller was handed, and passing it is what
    makes this answer a property of the ARGUMENTS. Until 2026-09-24 the two
    resolver calls below took a relative path and no root, so the overlay half
    of each came from `HEADING_OS_DATA`. MEASURED that day in a worktree, this
    scanner run twice with byte-identical `--data-root` and `--engine-root` and
    only the variable differing: 2867 references in scope against 2093, 140
    gated unresolved against 81, 72 gated pins against 42, both runs exit 0. The
    mechanism is the RULE SET, not the tree inference: the overlay's own
    `config/routing-map-private.yaml` carried 208 of the map's 404 rules and was
    read from that variable, so pointing it elsewhere silently deleted the
    clamps and 59 findings with them. Since HOS-232 that file is not read; the
    scope input that replaced it, `legacy_data_addresses`, is read from the
    argument.

    Two independent clauses, asked in this order:

    1. The resolver answers `private`: the path is in the data repository, or
       its first segment is a directory only the overlay has (HOS-232; until
       then, an explicit `private` rule, all of them in the overlay's half of
       the map).
    2. The resolver REFUSES it, as it does a path in neither tree, and its
       path lies under an entry of `overlay_only`, which the caller builds
       from the overlay's top-level names and `legacy_data_addresses`. A refusal is never
       read as `private`; it is read as "not placed", and only this clause can
       claim it.

    Until 2026-09-25 a third destination, `corporate`, was excluded here and
    counted, because corporate content lived in a separate repository on an
    executive workspace. HOS-132 retired the destination, and a map that still
    declares one is refused by `classification-health.py`.
    """
    try:
        return get_routing_destination(path, data_root=data_root) == "private"
    except RoutingRefused as exc:
        # Neither tree places it (HOS-232: no data rule names a legacy
        # address any more). Clause 2 decides; the refusal is not read as
        # private.
        return exc.reason != "escape" and any(
            path == entry.rstrip("/") or path.startswith(entry.rstrip("/") + "/")
            for entry in overlay_only)


# ============================================================
# The new tree -- labelled, and empty on purpose
# ============================================================

class NewTree(NamedTuple):
    """What the migration manifest says about the nine-family tree.

    `unmoved` holds every `target_dir` whose `state` column reads `not-moved`,
    and ONLY those. A reference into one of them is UNPOPULATED BY DEFINITION,
    whatever the filesystem says: a destination created ahead of its records
    exists and is empty, so `exists()` cannot tell it from a populated one.

    Every other state (`moving`, `not-for-migration`, `moved`) goes to the disk,
    because records have arrived there and the manifest is the stale layer.
    That is why the test at `load_new_tree` is `== STATE_NOT_MOVED` rather than
    a list: a new state joins the vocabulary without a line of code here
    changing. Until 2026-09-18 it was `state != "moved"`, which called
    populated `moving` targets empty.
    """

    families: frozenset[str]
    unmoved: tuple[str, ...]
    source: str          # the manifest that was read, or "" when absent

    def covers(self, rel: str) -> str | None:
        """The `not-moved` target this path falls inside, or None."""
        for target in self.unmoved:
            if rel == target or rel.startswith(target + "/"):
                return target
        return None


def load_new_tree(data_root: Path) -> NewTree:
    """Read `family-locations.tsv`, or return an empty manifest.

    ABSENT IS NOT AN ERROR and it is not a pass either. With no manifest the
    disk test in `_populated()` carries the whole second direction on its own,
    and `print_human` says which mechanism answered. Refusing here would make
    this scanner unusable before the migration starts and after it finishes,
    which are the two states it has to work in.

    Located through `resolve.manifest_dir()`, never by joining a literal, so
    this reader and the seam every other caller goes through read the same copy
    of the table.
    """
    try:
        path = manifest_dir(data_root) / LOCATIONS_FILENAME
    except NoManifestOnThisTree:
        return NewTree(frozenset(), (), "")
    try:
        rows = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return NewTree(frozenset(), (), "")
    if not rows:
        return NewTree(frozenset(), (), "")
    header = rows[0].split("\t")
    try:
        i_family = header.index("family")
        i_target = header.index("target_dir")
        i_state = header.index("state")
    except ValueError:
        return NewTree(frozenset(), (), "")
    families, unmoved = set(), []
    for row in rows[1:]:
        cell = row.split("\t")
        if len(cell) <= max(i_family, i_target, i_state):
            continue
        families.add(cell[i_family])
        # `== NOT_MOVED`, never `!= MOVED`. See `NewTree`: a `moving` target is
        # receiving records, so the disk is the fresher layer and the question
        # goes to `_populated()`. An unrecognised state falls to the disk too,
        # which is the direction that cannot invent an emptiness.
        if cell[i_state] == STATE_NOT_MOVED:
            unmoved.append(cell[i_target])
    # Longest first, so `covers()` names the most specific target.
    return NewTree(frozenset(families), tuple(sorted(unmoved, key=len, reverse=True)),
                   str(path))


# ============================================================
# Resolution -- where `exists()` is not the test
# ============================================================

# A path that names a SHAPE rather than a file. Documentation writes
# `outputs/intel/osint/YYYY-MM-DD-<target>.md` to state a naming convention, and  # leak-guard: ok (scanner vocabulary)
# the anchor pattern's character class stops at the `<`, leaving a stub that of
# course resolves to nothing. MEASURED 2026-09-12, this is 51 paths set aside
# beside 115 real findings, and every one of the 51 is correct prose.
#
# Detected mechanically and reported as its own status, never dropped in
# silence. The risk of a filter like this is that it hides a real path, so it is
# deliberately narrow (a literal date placeholder, a literal ellipsis, or a
# trailing separator left by the truncation) and the count is printed beside the
# findings so anyone can see how much it removed.
_PLACEHOLDER = re.compile(r"YYYY|MM-DD|\.\.\.|[-_]$")


def is_placeholder(rel: str) -> bool:
    """True when this path names a naming convention, not a file."""
    return bool(_PLACEHOLDER.search(rel))


# A file whose whole job is to say the directory holding it is empty on purpose.
# Every one of the nine new family roots carries one, and nothing else. Counting
# it as content is the second version of the same fail-open the module docstring
# opens with: `exists()` was defeated by the directory being created, and "holds
# a file" is defeated by the README that announces the directory holds nothing.
#
# Matched by NAME at the directory's own root only, never at depth, so a real
# `canon/principle/README.md` beside real records still reads as populated  # leak-guard: ok (scanner vocabulary)
# through its siblings.
_EMPTY_MARKER = "README.md"


def _populated(directory: Path) -> bool:
    """True when `directory` holds content, as opposed to holding a sign.

    Content is any regular file at any depth, EXCEPT a `README.md` sitting at
    the directory's own root with nothing else beneath it. See `_EMPTY_MARKER`.

    Short-circuits on the first real file, so the walk is cheap on a populated
    tree and pays for itself only on the empty one, which is small by
    definition.

    `os.walk` rather than `rglob`, for the `onerror` hook: an unreadable
    subdirectory would otherwise be skipped in silence and this function would
    answer "no content" for a tree it could not read. It re-raises instead, and
    the caller records the reference as unreadable.
    """
    def _raise(exc: OSError) -> None:
        raise exc

    root = str(directory)
    for dirpath, _dirnames, filenames in os.walk(directory, onerror=_raise):
        for name in filenames:
            if dirpath == root and name == _EMPTY_MARKER:
                continue
            return True
    return False


def resolve(data_root: Path, rel: str, new_tree: NewTree) -> tuple[str, str]:
    """(`ok` | `missing` | `unpopulated` | `placeholder` | `unreadable`, why).

    THE THREE OUTCOMES ARE THE POINT OF THIS FUNCTION, and `unpopulated` is the
    one the migration turns on. A reference can fail in two different ways and a
    guard that collapses them is useless in one of the two:

      missing      nothing is at that path. It announces itself.
      unpopulated  the path is there, correctly named, and holds no content.
                   It announces nothing, which is what makes it worse.

    Two independent mechanisms answer the second, and the AUTHORITATIVE one is
    asked first:

      manifest  `family-locations.tsv` says this target's `state` is
                `not-moved`, and that value alone. Then the reference is
                unpopulated BY DEFINITION, and no filesystem question is asked
                at all. This is the mechanism that survives whatever the tree
                gets seeded with. Every other state, `moving` and
                `not-for-migration` included, goes to the disk arm below: both
                describe a target that already holds records.
      disk      no manifest row covers it, so `_populated()` decides. That test
                excludes a lone root `README.md`, because every new family root
                carries exactly one and counting it as content would defeat the
                check the same way `exists()` was defeated.

    Both are kept. The manifest can go stale and the disk cannot see intent, so
    neither alone is enough, and the mechanism that answered travels out with
    the verdict rather than being inferred from the result.
    """
    # A match with no child segment carries less evidence than one with a path
    # under it. `canon/` in a rule is the family root and exactly what this
    # scanner is for; `research/` in a table of a notebook's relative output
    # directories is not a data-root path at all, and six of the nine new family
    # names are ordinary English words, so the collision is structural rather
    # than bad luck. It still GATES -- dropping it would lose the commonest way
    # prose names one of the new families -- but the verdict says which class it
    # is, so a reader triaging the report can see it without opening the file.
    # A logical NAME is answered by the vocabulary that owns it, never by the
    # disk. Dispatched here rather than at the one call site so that every
    # caller of `resolve()` gets the right machinery, which is the same reason
    # `scripts/utils/data_path.py` is one front door rather than three.
    if rel.startswith((RECORD_SCHEME, RUNTIME_SCHEME)):
        return resolve_address(rel)

    bare = "bare-root reference; " if "/" not in rel else ""

    covering = new_tree.covers(rel)
    if covering is not None:
        return "unpopulated", f"{bare}manifest: {covering} is not-moved"

    target = data_root / rel
    try:
        if target.is_file():
            return "ok", "file"
        if target.is_dir():
            if _populated(target):
                return "ok", f"{bare}directory holds content"
            # Which of the two it is matters to whoever reads the report: a bare
            # empty directory is a stub, while one holding only the marker was
            # stood up on purpose and is waiting for its content.
            if (target / _EMPTY_MARKER).is_file():
                return "unpopulated", f"{bare}holds nothing but {_EMPTY_MARKER}"
            return "unpopulated", f"{bare}exists and holds no file at any depth"
    except OSError as exc:
        return "unreadable", f"{type(exc).__name__}: {exc}"
    # Asked LAST, never first. A placeholder that happens to exist on disk is an
    # `ok`, and asking the filesystem before the pattern keeps this class as
    # narrow as it can be: only a path that resolves to nothing AND names a
    # shape is set aside.
    if is_placeholder(rel):
        return "placeholder", f"{bare}names a naming convention, not a file"
    return "missing", f"{bare}nothing at that path"


# ============================================================
# Scan
# ============================================================

class ScanResult(NamedTuple):
    """A scan, plus the corpus size that produced it.

    The findings alone cannot be read: an empty `unresolved` means a clean tree
    OR a scan that inspected nothing, and those two have to be told apart before
    anything prints OK. The counts travel with the result so no caller can
    forget to ask.
    """

    unresolved: dict[str, list[Reference]]   # path -> sites, status != ok
    status: dict[str, str]                   # path -> ok|missing|unpopulated|...
    why: dict[str, str]                      # path -> which mechanism decided
    references: list[Reference]              # every in-scope reference
    files_read: int
    references_seen: int
    in_scope: int
    binary: list[tuple[str, str]]            # (file, why) -- holds no prose to read
    unparsed: list[tuple[str, str]]          # (file, why) -- Python that would not parse
    # In-scope Python literals on a line carrying `INTENTIONAL_MARKER`. Kept
    # out of `references` and every count built on it, and read by one thing
    # only: the section 6 prohibition. See `PinFinding`.
    marked: list[Reference]

    def sites(self, path: str, *, fixtures: bool) -> list[Reference]:
        """The sites naming `path`, from fixtures or from real engine prose."""
        return [r for r in self.unresolved.get(path, []) if r.is_fixture is fixtures]

    def gated_paths(self, *, include_tests: bool) -> set[str]:
        """Unresolved paths this run is willing to fail on.

        Two exclusions, both counted and both listed elsewhere in the report.

        A path whose status is `placeholder` names a naming convention rather
        than a file, so nothing is wrong when it resolves to nothing.

        A path named ONLY from `tests/` is a sandbox name, excluded unless
        `--include-tests`. A path named from BOTH a fixture and real prose stays
        in: the real site is what fails, and dropping it because a test happens
        to mention the same string would be the silent narrowing this whole file
        argues against.
        """
        candidates = {p for p in self.unresolved if self.status[p] != "placeholder"}
        candidates -= self.described_only(include_tests=include_tests)
        if include_tests:
            return candidates
        return {p for p in candidates
                if any(not r.is_fixture for r in self.unresolved[p])}

    def described_only(self, *, include_tests: bool = False) -> set[str]:
        """Unresolved paths named ONLY by a docstring or a message (2026-09-14).

        The gate exists because a sentence in a skill or a rule INSTRUCTS a
        write, and an instruction into an empty new-tree directory is the
        silent orphaning this file is about. A Python docstring describing the
        migration, or a print naming a directory to a human, instructs no
        write and reaches no filesystem; failing the tree on it teaches people
        to stop describing. Those roles are derived by `_RoleFinder`, and a
        path is set aside here only when EVERY site naming it is one of them:
        one prose or `path` site beside them keeps the path gated, exactly as
        one live site beside a fixture does. Fixture sites are set aside
        first unless `include_tests`, the same order `gated_paths` applies to
        them: a fixture's prose beside a live docstring is still a sandbox.
        Listed in the report as `described_only_unresolved`, never dropped.

        ONE EXCEPTION, added 2026-09-21: a path that names a Python module and
        is `missing` is never set aside, whatever role its sites carry. The
        premise above is that prose "instructs no write and reaches no
        filesystem", and for a directory or an example data file that holds. For
        a `.py` under the data root it does not: the engine names one only to
        tell a reader to RUN it, and a reader who runs a path that is not there
        gets nothing back. MEASURED 2026-09-21 before this clause, the
        executive provisioner's old address was `missing`, was named from two
        engine docstrings and one printed command, and was set aside by every
        one of them; the tool had sat elsewhere for a week.

        Deliberately narrow, and the narrowness is the point. `missing` only,
        never `unpopulated` or `placeholder`: a directory that exists and is
        empty is the transition state this whole gate was built around. MEASURED
        the same day, this clause moves exactly three of the 123 described-only
        paths into the verdict, and the 120 it leaves alone are invented
        examples (`outputs/x`, `crm/contacts/jane.md`, `threads/foo.md`) that (leak-guard: ok, scanner vocabulary)
        turning this gate off wholesale would have surfaced as findings.
        """
        out = set()
        for path, refs in self.unresolved.items():
            sites = refs if include_tests else [r for r in refs if not r.is_fixture]
            if not sites:
                continue
            if not all(r.role in (ROLE_DOCSTRING, ROLE_MESSAGE) for r in sites):
                continue
            if path.endswith(".py") and self.status.get(path) == "missing":
                continue
            out.add(path)
        return out


def engine_files(engine_root: Path) -> list[str]:
    """Every git-tracked file under `SUBTREES`, engine-relative.

    Git's index rather than a filesystem glob, so `.venv/`, `__pycache__/` and
    untracked scratch never enter the corpus, and so a staged-but-uncommitted
    file does. `IndexUnreadable` propagates: a listing that came back empty is a
    failure, and reading it as "no files to check" is how a scanner reports
    clean over a tree it never enumerated.
    """
    return [p for p in git_index_paths(engine_root) if p.startswith(SUBTREES)]


def scan(engine_root: Path, data_root: Path, rules: dict,
         new_tree: NewTree) -> ScanResult:
    """Read the engine subtrees, resolve every in-scope reference."""
    # Clause 2's prefixes: the overlay's own top-level names, plus the legacy
    # and shared-name addresses only the overlay's list names (HOS-232).
    overlay_only = (overlay_only_segments(engine_root, data_root)
                    | legacy_data_addresses(data_root))
    pattern = anchor_pattern(rules, overlay_only)
    overlay_prefix = overlay_prefix_pattern(data_root)
    helpers = helper_names(engine_root)
    references: list[Reference] = []
    marked: list[Reference] = []
    binary: list[tuple[str, str]] = []
    unparsed: list[tuple[str, str]] = []
    files_read = references_seen = 0

    for rel in engine_files(engine_root):
        # The section 6 exemptions QUOTE statements of engine Python verbatim, so
        # every address in them is a copy of a site this loop reads where it
        # lives. `pins_verdict` refuses an entry that matches no live statement,
        # which is what makes skipping the copy safe: no address can exist only
        # here. Read, it counted every quoted address a second time: MEASURED
        # 2026-09-25, 94 unresolved paths became 184.
        if rel == PINS_EXEMPTIONS_REL:
            continue
        full = engine_root / rel
        try:
            text = full.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # Fonts, logos, `.pptx` and LFS payloads. A file that does not decode
            # as UTF-8 holds no prose this scanner could have read, so it is not
            # a gap in coverage -- but it is recorded and counted anyway, because
            # a corpus that shrinks in silence is a narrowed check printing like
            # a complete one.
            binary.append((rel, f"{type(exc).__name__}: {str(exc)[:80]}"))
            continue
        files_read += 1
        if rel.endswith(".py"):
            try:
                found = extract_python(text, rel, pattern, helpers,
                                       overlay_prefix=overlay_prefix)
            except (SyntaxError, ValueError) as exc:
                unparsed.append((rel, f"{type(exc).__name__}: {str(exc)[:80]}"))
                continue
        else:
            if rel == REGISTRY_TEMPLATE_REL:
                try:
                    text = mask_registry_history(text)
                except yaml.YAMLError as exc:
                    # Read unmasked: the count rises, which is the loud side.
                    print(f"{rel}: does not compose as YAML, its under: values "
                          f"are counted as addresses this run: {exc}",
                          file=sys.stderr)
            found = extract_prose(text, rel, pattern,
                                  is_routing_map=(rel == ROUTING_MAP_REL),
                                  overlay_prefix=overlay_prefix)
        marked.extend(r for r in found if r.marked or r.assembled)
        found = [r for r in found if not (r.marked or r.assembled)]
        references_seen += len(found)
        references.extend(found)

    # An address is in scope BY ITS SCHEME. `in_data_scope` asks whether a first
    # segment is an overlay directory name, and `record:trace` is not a
    # directory; running it past that clause would drop every logical name the
    # extractor just went to the trouble of recognising.
    in_scope = [r for r in references
                if r.is_address or in_data_scope(r.path, overlay_only, data_root)]
    status: dict[str, str] = {}
    why: dict[str, str] = {}
    unresolved: dict[str, list[Reference]] = {}
    for ref in in_scope:
        if ref.path not in status:
            status[ref.path], why[ref.path] = resolve(data_root, ref.path, new_tree)
        if status[ref.path] != "ok":
            unresolved.setdefault(ref.path, []).append(ref)

    marked_in_scope = [r for r in marked
                       if r.is_address or in_data_scope(r.path, overlay_only, data_root)]
    return ScanResult(unresolved, status, why, in_scope, files_read,
                      references_seen, len(in_scope), binary, unparsed,
                      marked_in_scope)


# ============================================================
# Counts the migration asked for
# ============================================================

def helper_names(engine_root: Path) -> set[str]:
    """The data-root helper functions, read off the seam's own AST.

    Derived, not listed. A `get_*_dir()` added to `scripts/utils/workspace.py`
    tomorrow counts tomorrow, with nobody editing this file. A hand-written list
    would go stale the first time the seam grows, and then the "resolves through
    helpers" count would quietly measure the list instead of the tree.

    The HOS-5 front door is derived the same way and for the same reason: every
    top-level `data_*` function in `FRONT_DOOR_MODULE`, so a fourth operation
    added beside `data_path`, `data_read` and `data_read_required` counts the
    day it lands. The front door's own module counts itself, exactly as
    `scripts/utils/workspace.py` has always counted itself for calling its own
    `get_*_dir` helpers; the alternative is one rule for the seam and another
    for the door, and a gauge that measures two things is a gauge nobody can
    read.
    """
    names: set[str] = set()
    for rel in SEAM_MODULES:
        try:
            tree = ast.parse((engine_root / rel).read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                    node.name == "get_data_root"
                    or (node.name.startswith("get_") and node.name.endswith("_dir"))):
                names.add(node.name)
    try:
        door = ast.parse((engine_root / FRONT_DOOR_MODULE).read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return names
    for node in door.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                node.name.startswith("data_"):
            names.add(node.name)
    return names


def helper_users(engine_root: Path, files: list[str], helpers: set[str]) -> set[str]:
    """Files that CALL one of the helpers, asked of the AST for Python.

    Non-Python files are asked as text, because a skill or a rule naming
    `get_outputs_dir()` in prose is telling a reader to use the seam, which is
    the thing being counted.
    """
    users: set[str] = set()
    for rel in files:
        try:
            text = (engine_root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if rel.endswith(".py"):
            try:
                tree = ast.parse(text)
            except (SyntaxError, ValueError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id in helpers:
                    users.add(rel)
                    break
                if isinstance(node, ast.Attribute) and node.attr in helpers:
                    users.add(rel)
                    break
        elif any(f"{h}(" in text for h in helpers):
            users.add(rel)
    return users


def unruled_data_dirs(data_root: Path, overlay_only: frozenset[str],
                      legacy: frozenset[str]) -> dict:
    """Overlay top-level directories the legacy list does not name, by visibility.

    Advisory, outside the exit code, and split because the two halves mean
    opposite things.

      reached_anyway   overlay-only, so scope clause 2 sees it by its name.
      invisible        a name the engine tree ALSO has and the overlay's legacy
                       list (`legacy_data_addresses`) does not name, so a reference
                       into it that the resolver cannot place never reaches this
                       scan. This is the real blind spot.

    Until HOS-232 the test was "no routing rule names it"; the map carries no
    data rule since, so that test would name every directory.
    """
    reached, invisible = [], []
    for entry in sorted(data_root.iterdir()):
        if not entry.is_dir() or entry.name.startswith(".") or entry.name in legacy:
            continue
        (reached if entry.name in overlay_only else invisible).append(entry.name)
    return {"reached_anyway": reached, "invisible": invisible}


def subtree_of(rel: str) -> str:
    """The `SUBTREES` entry a file belongs to."""
    for prefix in SUBTREES:
        if rel.startswith(prefix):
            return prefix
    return "(other)"


def build_report(engine_root: Path, data_root: Path, result: ScanResult,
                 new_tree: NewTree, *, include_tests: bool) -> dict:
    """The machine-readable report, and the source of every printed number."""
    files = engine_files(engine_root)
    all_tracked = git_index_paths(engine_root)
    helpers = helper_names(engine_root)
    real = [r for r in result.references if not r.is_fixture]

    per_subtree: dict[str, dict] = {}
    for prefix in SUBTREES:
        refs = [r for r in result.references if subtree_of(r.file) == prefix]
        bad = [r for r in refs if result.status.get(r.path) != "ok"]
        per_subtree[prefix] = {
            "files_in_subtree": sum(1 for f in files if f.startswith(prefix)),
            "files_naming_data_paths": len({r.file for r in refs}),
            "references": len(refs),
            "unresolved_references": len(bad),
            "unresolved_paths": sorted({r.path for r in bad}),
        }

    by_status: dict[str, list[str]] = {}
    for path, st in result.status.items():
        by_status.setdefault(st, []).append(path)

    gated = result.gated_paths(include_tests=include_tests)
    fixture_only = sorted(set(result.unresolved) - gated)
    new = sorted(p for p in gated if p not in BASELINE)

    def _split(key: str) -> dict:
        paths = sorted(by_status.get(key, []))
        return {
            "gated": [p for p in paths if p in gated],
            "fixture_only": [p for p in paths if p in set(fixture_only)],
        }

    return {
        "engine_root": str(engine_root),
        "data_root": str(data_root),
        "include_tests_in_verdict": include_tests,
        "corpus": {
            "files_listed": len(files),
            "files_read": result.files_read,
            "files_binary_not_read": len(result.binary),
            "python_files_unparsed": len(result.unparsed),
            "references_extracted": result.references_seen,
            "references_in_scope": result.in_scope,
            "references_from_test_fixtures": result.in_scope - len(real),
            "distinct_paths_in_scope": len(result.status),
        },
        # Both scopes are published because the two answer different questions
        # and the migration brief quoted numbers from the wider one. `subtrees`
        # is what this scanner reads; `whole_tree` is every tracked file. A
        # single number here would have been compared against the other scope
        # and read as a disagreement about the tree rather than about the scope.
        "counts": {
            "scope_subtrees": {
                "engine_files_naming_a_data_path":
                    len({r.file for r in result.references}),
                "engine_files_naming_a_data_path_excluding_tests":
                    len({r.file for r in real}),
                "engine_files_resolving_through_helpers":
                    len(helper_users(engine_root, files, helpers)),
                "python_files_with_a_hardcoded_data_literal":
                    len({r.file for r in result.references
                         if r.kind == "python-literal"}),
                "python_files_with_a_hardcoded_data_literal_excluding_tests":
                    len({r.file for r in real if r.kind == "python-literal"}),
            },
            "scope_whole_tree": {
                "files_tracked": len(all_tracked),
                "engine_files_resolving_through_helpers":
                    len(helper_users(engine_root, all_tracked, helpers)),
            },
            "helpers_derived": sorted(helpers),
        },
        "new_tree": {
            "manifest": new_tree.source,
            "families": sorted(new_tree.families),
            "not_moved_targets": len(new_tree.unmoved),
        },
        "status_counts": {k: len(v) for k, v in sorted(by_status.items())},
        "missing": _split("missing"),
        "unpopulated": _split("unpopulated"),
        "unreadable": _split("unreadable"),
        "unknown_address": _split(STATUS_UNKNOWN_ADDRESS),
        "placeholder": sorted(by_status.get("placeholder", [])),
        "why": {p: result.why[p] for p in sorted(result.unresolved)},
        "fixture_only_unresolved": fixture_only,
        "described_only_unresolved": sorted(result.described_only(include_tests=include_tests)),
        "new": new,
        "baseline": BASELINE,
        "stale_baseline": sorted(p for p in BASELINE if p not in result.unresolved),
        "per_subtree": per_subtree,
        "unruled_data_dirs": unruled_data_dirs(
            data_root, overlay_only_segments(engine_root, data_root),
            frozenset(e.split("/")[0] for e in legacy_data_addresses(data_root))),
        "binary_not_read": [{"file": f, "why": w} for f, w in sorted(result.binary)],
        "python_unparsed": [{"file": f, "why": w} for f, w in sorted(result.unparsed)],
        # How the Python literals are consumed, and how many of them PIN a
        # record (a live literal that constructs a path), published so the
        # narrowing is a number rather than a quiet one.
        "roles": {
            "python_literal_sites_by_role": {
                role: sum(1 for r in result.references
                          if r.kind == "python-literal" and r.role == role)
                for role in ROLES if role != ROLE_PROSE
            },
            "python_literal_sites_that_pin": sum(
                1 for r in result.references if r.kind == "python-literal" and r.pins),
            "python_files_with_a_pinning_literal": sorted(
                {r.file for r in result.references if r.pins}),
        },
        "sites": [
            {"path": p, "file": r.file, "line": r.line, "kind": r.kind,
             "role": r.role, "fixture": r.is_fixture, "status": result.status[p],
             "why": result.why[p]}
            for p, refs in sorted(result.unresolved.items()) for r in sorted(refs)
        ],
    }


# ============================================================
# Artifacts
# ============================================================

def write_artifacts(report: dict, result: ScanResult, out_dir: Path) -> list[Path]:
    """The JSON report, written atomically so a reader never sees half a file.

    Until 2026-10-16 this also wrote a Python-literal work list and a
    `sites.tsv` for the HEADING OS 2 mover, retired 2026-09-28; the report's
    `sites` carries the unresolved sites.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "data-path-references.json"
    atomic_write_text(report_path, json.dumps(report, indent=2) + "\n")
    return [report_path]


# ============================================================
# Output
# ============================================================

def refuse(reason: str, *, as_json: bool) -> int:
    """Print a refusal in the caller's format and return exit code 2.

    `--json` gets an object carrying `error`. A machine consumer parsing stdout
    would otherwise read the absence of findings as the absence of problems,
    which is the same fail-open the rest of this file is about.
    """
    if as_json:
        print(json.dumps({"error": reason, "unresolved": None, "new": None}, indent=2))
    else:
        print(f"{RED}{BOLD}REFUSED{RESET} -- {reason}", file=sys.stderr)
    return 2


def print_human(report: dict, result: ScanResult) -> None:
    c = report["corpus"]
    sub = report["counts"]["scope_subtrees"]
    whole = report["counts"]["scope_whole_tree"]
    print(f"{BOLD}{CYAN}Engine references into the data overlay{RESET}")
    print(f"{GRAY}engine: {report['engine_root']}")
    print(f"data:   {report['data_root']}{RESET}\n")

    print(f"{BOLD}Corpus{RESET}")
    print(f"  {c['files_read']} of {c['files_listed']} tracked file(s) read across "
          f"{len(SUBTREES)} subtree(s)")
    print(f"  {c['files_binary_not_read']} binary (no prose to read), "
          f"{c['python_files_unparsed']} Python file(s) that would not parse")
    print(f"  {c['references_in_scope']} of {c['references_extracted']} extracted "
          f"reference(s) in data scope (the resolver, the overlay's own "
          f"names, or its legacy-data-addresses list)")
    print(f"  of those, {c['references_from_test_fixtures']} come from "
          f"{FIXTURE_SUBTREE} and are sandbox paths, not live references")
    print(f"  {c['distinct_paths_in_scope']} distinct data path(s)\n")

    print(f"{BOLD}Counts{RESET} {GRAY}(measured here, never quoted from memory){RESET}")
    print(f"  files naming a data path ............ "
          f"{sub['engine_files_naming_a_data_path']:>4}  "
          f"{GRAY}({sub['engine_files_naming_a_data_path_excluding_tests']} "
          f"excluding {FIXTURE_SUBTREE}){RESET}")
    print(f"  files resolving through helpers ..... "
          f"{sub['engine_files_resolving_through_helpers']:>4}  "
          f"{GRAY}({whole['engine_files_resolving_through_helpers']} over all "
          f"{whole['files_tracked']} tracked files){RESET}")
    print(f"  Python files w/ hard-coded literal .. "
          f"{sub['python_files_with_a_hardcoded_data_literal']:>4}  "
          f"{GRAY}({sub['python_files_with_a_hardcoded_data_literal_excluding_tests']} "
          f"excluding {FIXTURE_SUBTREE}){RESET}\n")

    nt = report["new_tree"]
    print(f"{BOLD}The family tree (migration manifest){RESET}")
    if nt["manifest"]:
        print(f"  {len(nt['families'])} famil(ies), {nt['not_moved_targets']} "
              f"target(s) still `not-moved`, per")
        print(f"  {GRAY}{nt['manifest']}{RESET}")
    else:
        print(f"  {YELLOW}no migration manifest found; the second direction "
              f"rests on the disk test alone{RESET}")
    print()

    st = report["status_counts"]
    print(f"{BOLD}Resolution{RESET}  "
          f"{GREEN}ok {st.get('ok', 0)}{RESET}  {RED}missing {st.get('missing', 0)}{RESET}  "
          f"{YELLOW}unpopulated {st.get('unpopulated', 0)}{RESET}  "
          f"{GRAY}placeholder {st.get('placeholder', 0)}  "
          f"unreadable {st.get('unreadable', 0)}{RESET}")
    print(f"{GRAY}Three outcomes, not two. `missing` announces itself. `unpopulated` is a")
    print("path that is THERE, correctly named, holding no content, and it announces")
    print("nothing, which is what makes it the worse of the two. Two mechanisms")
    print("answer it and the manifest is asked first: a target whose state is")
    print("`not-moved` is unpopulated by definition, no filesystem question asked.")
    print("Otherwise the disk decides, counting a lone root README.md as the sign it")
    print(f"is rather than as content.{RESET}\n")

    for label, key, colour in (("MISSING", "missing", RED),
                               ("UNPOPULATED", "unpopulated", YELLOW),
                               ("UNREADABLE", "unreadable", RED),
                               ("UNKNOWN ADDRESS", "unknown_address", RED)):
        paths = report[key]["gated"]
        if not paths:
            continue
        print(f"{BOLD}{colour}{label}{RESET} ({len(paths)})")
        for path in paths:
            reason = BASELINE.get(path)
            tag = f"  {GREEN}baseline: {reason}{RESET}" if reason else ""
            sites = result.sites(path, fixtures=False)
            print(f"  {BOLD}{path}{RESET} [{len(sites)}] "
                  f"{GRAY}{report['why'].get(path, '')}{RESET}{tag}")
            for ref in sorted(sites):
                print(f"    {GRAY}{ref.file}:{ref.line} ({ref.kind}){RESET}")
        print()

    if report["placeholder"]:
        print(f"{GRAY}{len(report['placeholder'])} unresolved path(s) name a "
              f"NAMING CONVENTION rather than a file (a `YYYY-MM-DD` stub, an "
              f"ellipsis, or a prefix the extractor truncated at a `<`). Correct "
              f"prose, so not counted. Full list in --json.{RESET}\n")

    fixture_only = report["fixture_only_unresolved"]
    if fixture_only:
        verb = "counted" if report["include_tests_in_verdict"] else "NOT counted"
        print(f"{GRAY}{len(fixture_only)} further unresolved path(s) are named only "
              f"from {FIXTURE_SUBTREE} and are {verb} in the verdict: a test builds "
              f"its own overlay under tmp_path, so the path is a sandbox name. Full "
              f"list in --json and in the written report; --include-tests folds "
              f"them in.{RESET}\n")

    print(f"{BOLD}Per engine subtree{RESET}")
    for prefix, row in report["per_subtree"].items():
        print(f"  {prefix:<18} {row['files_naming_data_paths']:>4} of "
              f"{row['files_in_subtree']:>4} file(s) naming "
              f"{row['references']:>5} ref(s), {row['unresolved_references']:>4} unresolved")

    unruled = report["unruled_data_dirs"]
    if unruled["reached_anyway"] or unruled["invisible"]:
        print(f"\n{YELLOW}Overlay directories the legacy-addresses list does not name{RESET}")
    if unruled["reached_anyway"]:
        print(f"  reached anyway: {', '.join(unruled['reached_anyway'])}")
        print(f"{GRAY}    Only the engine has no directory of that name, so this scanner")
        print("    sees them by their name alone.")
        print(f"{RESET}", end="")
    if unruled["invisible"]:
        print(f"  {RED}invisible: {', '.join(unruled['invisible'])}{RESET}")
        print(f"{GRAY}    The engine has a directory of the same name, so the name alone")
        print("    settles nothing, and the overlay's legacy-addresses list does not name")
        print(f"    them. Unplaceable references into them never reach this scan.{RESET}")

    if report["python_unparsed"]:
        print(f"\n{RED}Python that would not parse, so was not checked "
              f"({len(report['python_unparsed'])}):{RESET}")
        for row in report["python_unparsed"]:
            print(f"  {row['file']}: {row['why']}")

    if report["stale_baseline"]:
        print(f"\n{YELLOW}Baseline entries that no longer match anything "
              f"(drop them):{RESET}")
        for path in report["stale_baseline"]:
            print(f"  {path}")


# ============================================================
# The count ratchet
# ============================================================

def read_ratchet(engine_root: Path, ratchet: Ratchet = UNRESOLVED_RATCHET) -> int | None:
    """The stored ceiling, or None when the file is absent or unreadable.

    None is "no floor has been set", which `--ratchet` treats as a refusal
    rather than a pass: a gate whose baseline vanished must not report clean.
    """
    path = engine_root / ratchet.rel
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    value = data.get(ratchet.key) if isinstance(data, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def write_ratchet(engine_root: Path, count: int, previous: int | None,
                  ratchet: Ratchet = UNRESOLVED_RATCHET) -> Path:
    """Store the ceiling. Shrink-only: a rise is refused, never written.

    Atomic per the workspace rule for persistent state, so a kill between write
    and rename cannot leave the gate with half a number and no floor at all.

    Through `scripts/utils/atomic.atomic_write_text`, whose scratch name is not
    derived from the target's and which leaves nothing behind on a kill.
    """
    if previous is not None and count > previous:
        raise ScopeCollapsed(
            f"refusing to raise the ratchet from {previous} to {count}. This "
            f"file only ever shrinks; a rise means new debt, which is what the "
            f"gate exists to stop.")
    path = engine_root / ratchet.rel
    payload = {
        ratchet.key: count,
        "measured": datetime.now(get_default_tz()).date().isoformat(),
        "what": ratchet.counts,
    }
    atomic_write_text(path, json.dumps(payload, indent=2) + "\n")
    return path


def ratchet_verdict(engine_root: Path, ratchet: Ratchet, count: int, *,
                    as_json: bool) -> int:
    """Compare `count` against the stored ceiling.

    Returns an exit code: 2 when no floor is set, 1 when the count rose, 0
    otherwise. The "no floor" case is a REFUSAL rather than a pass: a ceiling
    file deleted to get past the gate turns it red, not green.
    """
    ceiling = read_ratchet(engine_root, ratchet)
    if ceiling is None:
        return refuse(
            f"no ratchet floor: {engine_root / ratchet.rel} is absent, "
            f"unreadable, or carries no integer {ratchet.key!r}. A gate with no "
            f"floor cannot report a pass. Set one with {ratchet.set_with}.",
            as_json=as_json)
    if count > ceiling:
        # "N OVER", never "N new one(s) since the floor was set": this check
        # holds an integer, not the floor's composition, so a subtraction
        # cannot say which sites are new (`.claude/rules/scope-claims.md`).
        print(f"\n{RED}{BOLD}FAIL{RESET} -- {count} {ratchet.noun}(s), above the "
              f"ceiling of {ceiling}, by {count - ceiling}.", file=sys.stderr)
        print(f"{YELLOW}That difference is arithmetic and says nothing about "
              f"WHEN any site arrived: this check holds an integer, not the "
              f"floor's composition. Resolve sites, or lower the floor only "
              f"after the number has genuinely fallen. This file never "
              f"rises.{RESET}", file=sys.stderr)
        return 1
    if count < ceiling:
        print(f"{GREEN}ratchet{RESET} -- {count} {ratchet.noun}(s), below the "
              f"ceiling of {ceiling}. Lower it.", file=sys.stderr)
    else:
        print(f"{GRAY}ratchet -- {count} {ratchet.noun}(s), at the ceiling.{RESET}",
              file=sys.stderr)
    return 0


def pins_verdict(engine_root: Path, findings: list[PinFinding], *,
                 as_json: bool) -> int:
    """HOS-5 § 6: exit 1 on any pinning statement no entry covers, or any stale entry.

    Exit 2 when the exemptions file is absent or malformed: a prohibition whose
    list of reviewed exceptions cannot be read has established nothing, and
    reading "no file" as "no exceptions" would refuse every honest statement
    while reading it as "all excepted" would pass everything.
    """
    try:
        entries = load_exemptions(engine_root / PINS_EXEMPTIONS_REL)
    except ExemptionsError as exc:
        return refuse(f"the section 6 exemptions cannot be read: {exc}",
                      as_json=as_json)
    by_key: dict[tuple[str, str], list[PinFinding]] = {}
    for f in findings:
        by_key.setdefault(f.key, []).append(f)
    uncovered_keys, stale = match_exemptions([f.key for f in findings], entries)
    uncovered = [by_key[k].pop(0) for k in uncovered_keys]
    if uncovered or stale:
        if uncovered:
            print(f"\n{RED}{BOLD}FAIL{RESET} -- {len(uncovered)} statement(s) in "
                  f"engine Python spell a Data Repository address (HOS-5 section 6):",
                  file=sys.stderr)
            for f in uncovered:
                first = f.text.strip().splitlines()[0][:100] if f.text.strip() else ""
                print(f"  {RED}{f.file}:{f.start}{RESET}  {', '.join(f.paths)}\n"
                      f"    {GRAY}{first}{RESET}", file=sys.stderr)
            print(f"{YELLOW}Resolve the address through the registry, "
                  f"`data_path(\"<name>\")`. A literal that must stay a literal "
                  f"gets an entry in {PINS_EXEMPTIONS_REL}: the file path, the "
                  f"statement's full text, and the reason it cannot be a "
                  f"name.{RESET}", file=sys.stderr)
        if stale:
            print(f"\n{RED}{BOLD}FAIL{RESET} -- {len(stale)} entr(ies) in "
                  f"{PINS_EXEMPTIONS_REL} match no statement: the code they "
                  f"reviewed changed or left. Re-review or remove:", file=sys.stderr)
            for e in stale:
                print(f"  {RED}{e.path}{RESET}  {GRAY}{e.statement.strip()[:100]}{RESET}",
                      file=sys.stderr)
        return 1
    print(f"{GREEN}section 6{RESET} -- 0 unexplained hard-coded data addresses; "
          f"{len(findings)} statement(s) spell one, each exempted with a reason "
          f"in {PINS_EXEMPTIONS_REL}.", file=sys.stderr)
    return 0


# ============================================================
# Entry point
# ============================================================

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Engine references into the data overlay.")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 on any unresolved reference not in BASELINE")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--write-report", action="store_true",
                    help="write the JSON report")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="where --write-report writes (default: the registry name ho2-migration-reports)")
    ap.add_argument("--data-root", type=Path, default=None,
                    help="resolve against this overlay instead of the configured one")
    ap.add_argument("--engine-root", type=Path, default=None,
                    help="scan this checkout instead of the one this script sits in")
    ap.add_argument("--include-tests", action="store_true",
                    help=f"let a path named only from {FIXTURE_SUBTREE} fail --check")
    ap.add_argument("--ratchet", action="store_true",
                    help=f"exit 1 when unresolved paths exceed {RATCHET_REL}")
    ap.add_argument("--write-ratchet", action="store_true",
                    help="store today's count as the ceiling (shrink-only)")
    ap.add_argument("--pins-check", action="store_true",
                    help=f"HOS-5 section 6: exit 1 on any hard-coded data address "
                         f"in engine Python not exempted, with a reason, in "
                         f"{PINS_EXEMPTIONS_REL}, and on any stale entry there")
    ap.add_argument("--skip-without-overlay", action="store_true",
                    help="exit 0 with a notice when there is no private overlay "
                         "to resolve against, instead of refusing")
    args = ap.parse_args(argv)

    engine_root = (args.engine_root.resolve() if args.engine_root
                   else engine_root_from_checkout())

    # Asked BEFORE the refusals below, because on a public clone this scanner
    # exits 2 by design ("it has nothing to say on a public clone") and an
    # automatic caller needs a way to distinguish "no overlay here" from "the
    # gate broke". Without this, wiring the scanner into any shared hook makes
    # every clone without the operator's private overlay fail on its first run.
    if args.skip_without_overlay and args.data_root is None and data_root_is_demo():
        print(f"{GRAY}data-path scan skipped: no private overlay on this tree, "
              f"so there is nothing to resolve references against.{RESET}",
              file=sys.stderr)
        return 0

    try:
        require_map_tree_agrees(engine_root)
        # The data root is settled FIRST: it is half of the routing map now, so
        # loading the map before it is known would read the overlay half from
        # the environment and hand the rest of this run a rule set the arguments
        # did not choose.
        data_root = require_data_root(args.data_root)
        rules = require_routing_map(engine_root, data_root)
    except ScopeCollapsed as exc:
        return refuse(str(exc), as_json=args.json)
    except ValueError as exc:
        # A `tree=` that contradicts an absolute path raises ValueError in the
        # resolver. Reported as a refusal (exit 2), never as a finding:
        # nothing was established. (Until HOS-232 this also caught a root with
        # no overlay rule file; nothing reads that file now.)
        return refuse(str(exc), as_json=args.json)

    new_tree = load_new_tree(data_root)
    try:
        result = scan(engine_root, data_root, rules, new_tree)
    except (ScopeCollapsed, IndexUnreadable) as exc:
        return refuse(str(exc), as_json=args.json)

    # Every refusal below is exit 2, never 0 and never 1. 0 is "every reference
    # resolved" and 1 is "one did not"; a scanner that inspected nothing has
    # established neither, and borrowing either code hands a machine consumer a
    # verdict no measurement backs.
    if result.files_read == 0:
        return refuse(
            f"no file under {', '.join(SUBTREES)} was read in {engine_root}, so "
            f"nothing was checked. A pass over an empty corpus is not a pass.",
            as_json=args.json)
    if result.in_scope == 0:
        return refuse(
            f"{result.files_read} file(s) read and {result.references_seen} "
            f"path reference(s) extracted, but none of them is in data scope: "
            f"neither the resolver, the overlay's own top-level names, nor its "
            f"{LEGACY_ADDRESSES_FILENAME} list claimed one, so nothing was "
            f"checked. An overlay or list that went missing silences this "
            f"scanner over everything.",
            as_json=args.json)

    report = build_report(engine_root, data_root, result, new_tree,
                          include_tests=args.include_tests)

    if args.write_report:
        if args.out_dir:
            out_dir = args.out_dir
        else:
            try:
                out_dir = data_path("ho2-migration-reports")
            except DataPathError as exc:
                return refuse(
                    f"--write-report has no --out-dir and the registry does not "
                    f"answer the report's home: {exc}", as_json=args.json)
        for path in write_artifacts(report, result, out_dir):
            print(f"{GREEN}wrote{RESET} {path}", file=sys.stderr)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_human(report, result)

    if args.check:
        new = report["new"]
        if new:
            print(f"\n{RED}{BOLD}FAIL{RESET} -- {len(new)} data path(s) named by "
                  f"engine files do not resolve:", file=sys.stderr)
            for path in new:
                sites = result.unresolved[path]
                where = ", ".join(f"{r.file}:{r.line}" for r in sorted(sites)[:3])
                print(f"  {RED}{path}{RESET} [{result.status[path]}]  ({where})",
                      file=sys.stderr)
            print(f"{YELLOW}Fix the reference, or add the path to BASELINE with "
                  f"the reason it should not resolve.{RESET}", file=sys.stderr)
            return 1
        print(f"\n{GREEN}OK{RESET} -- every one of "
              f"{report['corpus']['distinct_paths_in_scope']} data path(s) named "
              f"by the engine resolves to something populated.")

    # The count this file ratchets, read out of the report rather than
    # recomputed here, so the printed number and the gated number cannot be two
    # different measurements of one tree.
    if args.write_ratchet:
        try:
            path = write_ratchet(engine_root, len(report["new"]),
                                 read_ratchet(engine_root, UNRESOLVED_RATCHET),
                                 UNRESOLVED_RATCHET)
        except ScopeCollapsed as exc:
            return refuse(str(exc), as_json=args.json)
        print(f"{GREEN}wrote{RESET} {path}: {UNRESOLVED_RATCHET.key}="
              f"{len(report['new'])}", file=sys.stderr)
    if args.ratchet:
        code = ratchet_verdict(engine_root, UNRESOLVED_RATCHET, len(report["new"]),
                               as_json=args.json)
        if code != 0:
            return code
    if args.pins_check:
        return pins_verdict(engine_root, pin_findings(engine_root, result),
                            as_json=args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
