#!/usr/bin/env python3
"""memory_health.py - objective auto-memory defect computation (importable).

Pure, directory-parameterized computation of the mechanically-verifiable defects
in an auto-memory directory (a folder of `*.md` fact files plus a `MEMORY.md`
index). Extracted from the inlined logic in `scripts/prime-health-parallel.py`
so both that health panel and `scripts/memory-hygiene.py` share one
implementation.

This module READS ONLY. It never writes, merges, or deletes a memory file.
"Objective" here means deterministically checkable without judgement:
  - orphans       : a `*.md` fact file reachable through neither MEMORY.md nor
                    the Hindsight bank `main`. Established only when the bank
                    could be consulted AND names enough of the corpus to be
                    believed; see `orphans_evaluated`, `hindsight_record_names`
                    and `hindsight_coverage`. This module only GETs from the
                    bank; it never writes to it.
  - over_budget   : MEMORY.md exceeds the line budget (default 200)
  - over_byte_budget : MEMORY.md exceeds the harness's 25000 UTF-16 unit budget,
                    the axis on which the index is silently truncated
  - stale         : a fact file older than STALE_DAYS by mtime (advisory signal)

Consumed by:
  - scripts/prime-health-parallel.py (run_memory_health)
  - scripts/memory-hygiene.py
"""
from __future__ import annotations

import datetime
import logging
import math
import re
from pathlib import Path

from scripts.utils.claude_md_chain import (
    HARNESS_MAX_DEPTH,
    HOOK_STDOUT_UTF16_LIMIT,
    resolve_chain,
    user_instruction_root,
    utf16_units,
)
from scripts.utils.memory_expiry import LINK_PATTERN, _LINK_RE, index_link_targets
from scripts.utils.workspace import get_default_tz, get_workspace_root

logger = logging.getLogger(__name__)

# Budget + staleness thresholds (kept identical to the prior inlined values).
MEMORY_BUDGET_LINES = 200
STALE_DAYS = 45

# ---------------------------------------------------------------------------
# The harness's index budget, on the axis nothing here used to measure.
#
# THESE ARE VENDOR CONSTANTS, READ 2026-09-09 OUT OF ONE BUILD: Claude Code
# 2.1.266 (`~/.local/share/claude/versions/2.1.266`). They are not a published
# API, nothing promises they are stable, and we have not measured how they move
# between versions. Re-read them from the build in use rather than trusting this
# block; treat a mismatch as this file being out of date, not the harness.
#
# The loader, verbatim from that build:
#
#     var Yl="MEMORY.md", bD=200, MB=25000, ACe=200, NQ=4096;
#     function HBe(e){ let n=e.trim();
#       return {trimmed:n, lineCount: on(n,"\n")+1, byteCount: n.length} }
#     ... let p = o>bD, y = d>MB;                       // strictly greater
#         let L = p ? r.split("\n").slice(0,bD).join("\n") : r;
#         if (L.length>MB){ let k = L.lastIndexOf("\n",MB);
#                           L = L.slice(0, k>0 ? k : MB) }
#
# Three facts that block is the only source for, and each one matters here:
#
#   1. IT DOES NOT REFUSE, IT TRUNCATES. Lines past the 200th go first, then the
#      remainder is cut at the last newline before 25000. What disappears is the
#      END of the file. A warning is appended to the injected text, but the
#      content is already gone by then, so a budget check that only fires after
#      the fact reports a loss instead of preventing one.
#   2. `byteCount` is `n.length`, a JS string length, i.e. UTF-16 CODE UNITS.
#      Neither UTF-8 bytes nor code points. See `utf16_units` for the measured
#      gap and why each wrong answer fails in its own direction.
#   3. It measures the TRIMMED text, and `lineCount` is `newlines + 1`.
#
# `ACe=200` and `NQ=4096` sit in the same statement and are a DIFFERENT limit:
# 200 lines / 4096 units applied to a SINGLE recalled memory FILE, not to the
# index. They are deliberately not modelled here; do not read the shared `200`
# as one budget.
MEMORY_INDEX_UTF16_BUDGET = 25000

# The harness's own warning threshold on the worse of the two axes (`mos=0.8` in
# the same build). Read here as: the point at which the index needs restructuring
# work scheduled, while there is still room to do it deliberately.
MEMORY_INDEX_WARN_FRACTION = 0.8

# The one remedy this workspace will name. Operator decision, 2026-09-09:
# shortening the index labels is NEVER proposed, because the label is the
# retrieval signal and every past round of trimming cost recall quality. The
# index is ORDERED and never trimmed (see its own header); a check that told the
# reader to "condense" would be arguing with that rule instead of supporting it.
MEMORY_INDEX_REMEDY = (
    "reorder the sections so truncation eats the recoverable end, or move a "
    "section into its own file - never shorten the labels"
)


# `utf16_units` was written here and moved to `claude_md_chain` on 2026-09-20,
# re-exported under its old name so every importer keeps working. It moved
# because `.claude/hooks/session-start.py` now has to measure its own stdout
# against `HOOK_STDOUT_UTF16_LIMIT`, and that hook loads `claude_md_chain` and
# not this module. Two copies of a count is two answers to one question, and the
# second copy is the one that stops being fixed. The import
# sits at the top of this module with the rest, and the name is public here.


def measure_index(text: str) -> tuple[int, int]:
    """`(line_count, utf16_units)` for an index, computed the way the harness
    computes them: on the TRIMMED text, with the line count as newlines + 1.

    `str.strip()` stands in for JS `.trim()`. The two whitespace sets are not
    identical (U+FEFF is trimmed by JS and not by Python), which can shift the
    count by a character at the very edges of the file. That is noted rather
    than chased: it cannot move a verdict at any distance from the budget that
    a reader could act on.
    """
    trimmed = text.strip()
    return trimmed.count("\n") + 1, utf16_units(trimmed)


# ---------------------------------------------------------------------------
# The one-way door: an index that shrank, and a policy file nothing reaches.
#
# On 2026-09-20 the conduct rules moved out of the index into `CLAUDE.private.md`
# in the private overlay, reached through `CLAUDE.operational.md` by an `@` import
# chain, which is not truncated, while the index is cut from the END past 25000
# UTF-16 units with nothing saying so.
#
# The hazard is a HALF-DONE version of that move, and it is silent in both
# directions. Shrink the index to a router and forget to wire the policy file,
# and the rules of conduct leave the context with no error anywhere: the
# index is well under budget (which reads as health), the policy file exists on
# disk (which reads as done), and nothing asks whether anything reaches it.
#
# So the check is the CONJUNCTION, and it is asked of the import chain rather
# than of the filesystem, through the same resolver
# `.claude/hooks/session-start.py` injects with. One traversal, one answer: a
# reachability question answered twice is answered twice differently.
# ---------------------------------------------------------------------------

POLICY_LAYER_FILENAME = "CLAUDE.private.md"

# The file `CLAUDE.md` imports, and the root of the chain a YARD gets instead.
OPERATIONAL_LAYER_FILENAME = "CLAUDE.operational.md"

# MEASURED 2026-09-20 on the operator's overlay: `auto-memory/MEMORY.md` is 179  # leak-guard: ok (measurement: the address as it was on the day measured)
# lines and 25207 UTF-16 units, already past `MEMORY_INDEX_UTF16_BUDGET` (25000)
# on the axis the harness truncates. The floor is under HALF of that, so no
# ordinary edit reaches it and no amount of pruning individual rules does
# either; what reaches it is the deliberate removal of the bulk of the rules,
# which is exactly the move this guards. It is a trip point, never a target: an
# index ABOVE it is not thereby healthy, and `over_byte_budget` is the check
# that answers the other end.
MEMORY_INDEX_POLICY_FLOOR_UTF16 = 12000


def check_policy_layer(memory_dir, *, claude_md=None, policy_file=None,
                       operational_file=None, user_claude_md=None,
                       floor: int = MEMORY_INDEX_POLICY_FLOOR_UTF16) -> dict:
    """Is the conduct layer reachable, given how small the index has become?

    READS ONLY. Returns a dict; never raises. Shape:

        {
          "status": "ok" | "error" | "skipped",
          "ok": bool,                    # False only on "error"
          "index_utf16_units": int,
          "index_readable": bool,
          "floor": int,
          "below_floor": bool,
          "policy_file": str,
          "policy_reachable": bool | None,   # None when the chain was not asked
          "reached_via": str,            # see the vocabulary below
          "reached_via_path": str,       # the root whose chain answered
          "chain_root": str,
          "chain_files": list[str],
          "chain_problems": list[str],
          "hook_payload_utf16_units": int,   # 0 when the hook route was not asked
          "hook_payload_over_cap": bool,
          "note": str,                   # one line, the reason for the status
        }

    THREE ROUTES, AND `reached_via` NAMES WHICH ONE ANSWERED. Its vocabulary is
    `"CLAUDE.md"`, `"~/.claude/CLAUDE.md"`, `"hook"`, or `""` for none.

    1. **The project's own `CLAUDE.md`.** Asked first, and since 2026-09-20 the
       engine's copy deliberately carries no such import (the path would be
       machine-specific in a public repository), so on this workspace it
       normally answers no. It is still asked, because another operator's clone
       may wire it and a check that stopped asking would call their working
       setup broken.
    2. **The USER-level root**, `~/.claude/CLAUDE.md`, which is where the chain
       actually starts now. It is outside every project, so it reaches the main
       clone and a worktree identically. A check that did not know about it
       would report every correctly-configured yard as broken the moment the
       hook route went away, which is the direction this workspace is moving in.
    3. **The SessionStart hook**, `check_operational_layer` in
       `.claude/hooks/session-start.py`, rooted at the operational file and
       bounded where the hook bounds it. ASKED LAST AND CREDITED ONLY WHEN ITS
       PAYLOAD FITS. Hook stdout is capped at `HOOK_STDOUT_UTF16_LIMIT` UTF-16
       units (MEASURED 2026-09-20, `DJr` in Claude Code 2.1.277), past which the
       whole output is persisted to a file and only a ~2000-character preview
       reaches the session, with nothing printed to say so. Crediting that route
       for a payload it cannot carry is a vacuous pass: run from a worktree
       against the live tree before this change, this function answered
       `status: ok, reached_via: hook` over about 35,000 units of rules that
       arrive as a preview. So an over-cap hook-only route is an ERROR, and its
       message says the rules would arrive TRUNCATED rather than ABSENT, because
       the two look different in the transcript and need different repairs.

    The payload measured for route 3 is the hook's operational-layer bytes
    alone. That is a FLOOR and not the whole budget: the same hook prints the
    alert block, the CRM line and the thread panel into the same 10,000 units,
    so a payload that passes here can still be persisted in a live session.
    The hook itself measures the whole thing; this check cannot see the rest.

    THE THREE STATES, and which one this is has to be readable from the result
    rather than from this source:

    **skipped** -- there is no auto-memory directory and no index. That is a
    clone with no private overlay: `get_data_root()` lands on the empty
    `examples` sentinel, a directory the engine no longer ships.
    There is no index that could have shrunk, so there is nothing to say. This is
    the shape of defect `--skip-without-overlay` already exists in this
    repository to prevent, and it is answered structurally rather than by reading
    the environment, so the function's answer depends only on its arguments.

    **error** -- any of: the directory exists but the index is gone or could not
    be read; the index is under `floor` and the chain does not reach the policy
    file; or the chain could not be read at all. THE LAST ONE IS THE POINT. A
    check that resolves clean when its own input collapses is the defect
    `scripts/check-test-vacuity.py` exists to catch here, and an unreadable
    `CLAUDE.md` is precisely the state in which "the policy file is not
    reachable" would otherwise be indistinguishable from "nothing was asked".

    **ok** -- the index still carries its own weight (at or above `floor`), or
    it does not and the chain reaches the policy file. Both are honest passes
    and both must stay green; a guard that refuses everything satisfies every
    refusal test.

    `claude_md` defaults to this checkout's own `CLAUDE.md`, derived from the
    workspace root rather than from a constant, so a worktree asks about its own
    file. `policy_file` defaults to `POLICY_LAYER_FILENAME` beside the memory
    directory, which is where the overlay puts it.
    """
    memory_dir = Path(memory_dir)
    index = memory_dir / "MEMORY.md"
    if policy_file is None:
        policy_file = memory_dir.parent / POLICY_LAYER_FILENAME
    policy_file = Path(policy_file)
    if operational_file is None:
        operational_file = memory_dir.parent / OPERATIONAL_LAYER_FILENAME
    operational_file = Path(operational_file)
    if claude_md is None:
        claude_md = get_workspace_root() / "CLAUDE.md"
    claude_md = Path(claude_md)
    if user_claude_md is None:
        user_claude_md = user_instruction_root()
    user_claude_md = Path(user_claude_md)

    result = {
        "status": "ok",
        "ok": True,
        "index_utf16_units": 0,
        "index_readable": False,
        "floor": floor,
        "below_floor": False,
        "policy_file": str(policy_file),
        "policy_reachable": None,
        "reached_via": "",
        "reached_via_path": "",
        "chain_root": str(claude_md),
        "chain_files": [],
        "chain_problems": [],
        "hook_payload_utf16_units": 0,
        "hook_payload_over_cap": False,
        "note": "",
    }

    if not memory_dir.is_dir() and not index.exists():
        result["status"] = "skipped"
        result["note"] = (
            f"no auto-memory directory at {memory_dir}, so there is no index "
            f"that could have shrunk; this is a clone with no private overlay")
        return result

    if not index.is_file():
        result.update(status="error", ok=False, note=(
            f"{memory_dir} exists and {index} does not: the index is gone, and "
            f"an absent index carries no rule at all"))
        return result

    try:
        # `errors="ignore"`, matching every other read of this file in this
        # module: `UnicodeDecodeError` is a `ValueError` and not an `OSError`,
        # so a strict read would raise straight past the handler below and take
        # the whole check with it.
        text = index.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        result.update(status="error", ok=False, note=(
            f"the memory directory {memory_dir} exists but its index could not "
            f"be read ({exc}), so whether the conduct rules are still in it is "
            f"unknown, not fine"))
        return result

    result["index_readable"] = True
    _, units = measure_index(text)
    result["index_utf16_units"] = units
    result["below_floor"] = units < floor
    if not result["below_floor"]:
        result["note"] = (
            f"the index is {units} UTF-16 units, at or above the {floor} floor, "
            f"so it still carries the conduct rules itself")
        return result

    chain = resolve_chain(claude_md)
    files = list(chain.files)
    problems = list(chain.problems)
    if not chain.root_readable:
        result["chain_files"] = [str(f.path) for f in files]
        result["chain_problems"] = [p.describe() for p in problems]
        result.update(status="error", ok=False, note=(
            f"the index is {units} UTF-16 units, under the {floor} floor, and "
            f"{claude_md} could not be read, so nothing established whether "
            f"{policy_file.name} is reachable"))
        return result

    reached_via = "CLAUDE.md" if chain.holds(policy_file) else ""
    reached_via_path = claude_md if reached_via else None

    if not reached_via and user_claude_md.is_file():
        # Route 2. A full-depth walk, because this root is where the harness
        # itself starts: it is not an import of anything, so it sits at depth 0
        # exactly as a project `CLAUDE.md` does.
        user_chain = resolve_chain(user_claude_md)
        files.extend(f for f in user_chain.files if f.path not in set(chain.paths))
        problems.extend(user_chain.problems)
        if user_chain.holds(policy_file):
            reached_via = "~/.claude/CLAUDE.md"
            reached_via_path = user_claude_md

    hook_chain = None
    if not reached_via and operational_file.is_file():
        # Route 3. Rooted and bounded where the hook roots and bounds it: the
        # operational file sits at the harness's depth 1, so the chain below it
        # gets one level less than the harness's own ceiling, and a file this
        # check counted that a real session would never load would be the two
        # answers disagreeing in the other direction.
        reached = {f.path for f in files}
        hook_chain = resolve_chain(
            operational_file, max_depth=max(1, HARNESS_MAX_DEPTH - 1))
        files.extend(f for f in hook_chain.files if f.path not in reached)
        problems.extend(hook_chain.problems)
        payload_units = utf16_units(hook_chain.text)
        result["hook_payload_utf16_units"] = payload_units
        result["hook_payload_over_cap"] = payload_units > HOOK_STDOUT_UTF16_LIMIT
        if hook_chain.holds(policy_file) and not result["hook_payload_over_cap"]:
            reached_via = "hook"
            reached_via_path = operational_file

    result["chain_files"] = [str(f.path) for f in files]
    result["chain_problems"] = [p.describe() for p in problems]
    result["policy_reachable"] = bool(reached_via)
    result["reached_via"] = reached_via
    result["reached_via_path"] = str(reached_via_path) if reached_via_path else ""
    if reached_via:
        source = str(reached_via_path)
        if reached_via == "hook":
            source += (" (the route a worktree gets, injected by "
                       ".claude/hooks/session-start.py)")
        result["note"] = (
            f"the index is {units} UTF-16 units, under the {floor} floor, and "
            f"{policy_file.name} is reached from {source} through "
            f"{len(files)} file(s)")
        return result

    # The route exists and cannot carry the payload. A DIFFERENT defect from
    # having no route, and it has to read as one: the rules arrive TRUNCATED to
    # a preview rather than absent, so the transcript shows a marker and the
    # first ~2000 characters of policy, which reads like success.
    if hook_chain is not None and result["hook_payload_over_cap"] \
            and hook_chain.holds(policy_file):
        result.update(status="error", ok=False, note=(
            f"the index is {units} UTF-16 units, under the {floor} floor, and "
            f"{policy_file.name} is reachable ONLY through the SessionStart "
            f"hook, whose payload is {result['hook_payload_utf16_units']} "
            f"UTF-16 units against the harness's {HOOK_STDOUT_UTF16_LIMIT} cap "
            f"on hook stdout (MEASURED 2026-09-20, `DJr` in Claude Code "
            f"2.1.277). The conduct rules would arrive TRUNCATED to a preview, "
            f"not absent, and nothing in the session says so. The repair is to "
            f"root the chain in {user_claude_md}, which has no cap, not to "
            f"shorten what a yard receives"))
        return result

    result.update(status="error", ok=False, note=(
        f"the index is {units} UTF-16 units, under the {floor} floor, AND "
        f"{policy_file} is reachable through the @ import chain of none of "
        f"{claude_md}, {user_claude_md} or {operational_file} "
        f"({len(files)} file(s) reached). Both facts together mean the conduct "
        f"rules are in no place a session loads"))
    return result


# ---------------------------------------------------------------------------
# Volatile-hook guard (advisory) — enforces the memory-discipline convention:
# a MEMORY.md index hook names the TOPIC and points to the file; it must NOT
# quote a live/volatile value (a price, ceiling, offer, live count, live
# deadline, current status). Volatile values belong in the record body, read on
# demand — a hook that never quotes a live number cannot go stale into a wrong
# number (see .claude/rules/memory-discipline.md).
#
# The heuristic is deliberately HIGH-PRECISION, not high-recall: it targets the
# money/quantitative-state class that caused the stale-money-hook failure, and accepts that
# some volatile prose (bare live counts/dates with no money signal) is not
# caught. It is ADVISORY (never gates), so a rare false positive is a review
# nudge, not a build break.
#
# Volatile signals (flag) — precision-first money detection:
#   - currency:        €/$/£ or an ISO code (USD/EUR/GBP/AED/CHF) adjacent to a digit
#   - money magnitude: a 'k'/'K'/'m'/'M' thousands/millions suffix, ONLY when a
#                      money-context word is present in the same text (price, offer,
#                      seller, loan, mortgage, budget, deal, LTV, pipeline, ...).
# The money-context co-factor is what keeps SPEC magnitudes from false-flagging:
# "128k context", "1M-context", "5K display", "10k RPM", "i9-13900K", "~7-8B" carry
# a k/M/B token but NO money word, so they do not flag. A bare money-VOCABULARY
# signal was rejected earlier (it flagged "local ceiling ~7-8B"); here vocabulary is
# only a REQUIRED co-factor for a magnitude token, never a standalone trigger, so
# that false-positive class stays closed. "ceiling" is intentionally NOT a money
# word here.
#
# Recall is deliberately PARTIAL (see .claude/rules/memory-discipline.md): this
# guard closes the MONEY-hook class that caused the stale-money-hook failure. Non-money
# volatile prose (live dates like "due 2026-09-05", live counts, "70%", status) is
# NOT mechanically caught — that breadth is the always-on principle's job, not a
# reason to widen this heuristic into a false-positive machine. The guard is
# ADVISORY and never gates. It scans BOTH the MEMORY.md index hooks and each memory
# file's frontmatter `description:` (both are pointer-layer summaries that go stale).
_VH_CURRENCY_RE = re.compile(r"(?:[€$£]|\b(?:USD|EUR|GBP|AED|CHF)\b)\s?\d")
_VH_MAGNITUDE_RE = re.compile(r"\b\d+(?:\.\d+)?[kKmM]\b")
_VH_MONEY_CTX_RE = re.compile(
    r"\b(price|offer|seller|buyer|loan|mortgage|budget|deal|asking|valuation|"
    r"salary|revenue|ARR|cash|equity|fee|fees|deposit|rent|proceeds|pipeline|LTV)\b",
    re.IGNORECASE,
)
# One MEMORY.md index hook: "[Title](file.md) — note", up to the next ` · `
# separator or the end of the line. Pointers under threads/ are skipped (generated
# pointers to live records, not memory hooks); a bare filename OR a future
# memory-subdir hook is still scanned (do NOT skip on any '/').
#
# This matched from the BULLET until 2026-08-27: `^\s*-\s*\[[^\]]+\]\((...)\)`,
# which demands the link immediately after the dash. The index is grouped by
# subject, so a line reads `- Memory: [a](a.md) · [b](b.md)`, and the label
# between the bullet and the bracket made the whole line fail. Measured against
# the live index that day: 10 lines matched, out of 216 pointers present. The
# guard reported zero findings and was believed, because a scan of 5% of a corpus
# prints the same words as a scan of all of it.
#
# Pointer-at-a-time also fixes the misattribution: signals were read from the
# whole line while the reported target was the FIRST pointer on it, so a price in
# the fifth hook sent the operator to the first hook's file.
# `scripts/utils/memory_expiry.py` already solved this shape; this mirrors it.
# The tail stops at the next pointer as well as at ` · `. It was `[^·\n]*`,
# greedy, and `[`, `]`, `(` and `)` are all inside that class: on a line whose
# pointers are NOT separated by a middle dot, the first match swallowed the rest
# of the line, `finditer` resumed past the end, and every later pointer went
# unscanned and unreported. That is the misattribution the paragraph above says
# was fixed - it was fixed for the separated case only.
#
# It is composed from `memory_expiry._LINK_RE` rather than spelled again, and
# that is the third defect this pattern has carried. The flat `\[[^\]]*?\]` here
# could not cross the `]` of a label containing `args[0]`, so the pointer whose
# label reads "args[0] on Path.open, args[1] on open" was invisible to this scan
# and to nothing else: MEASURED 2026-09-10 on the live index, this pattern found
# 339 pointers where `_LINK_RE` finds 340. The identical repair had already
# landed in `memory_expiry` on 2026-09-02, and its comment says so; it landed in
# one of the three copies. One record therefore went eight days never once
# checked for a volatile value, and the scan reported clean over it.
_VH_POINTER_RE = re.compile(
    _LINK_RE.pattern + r"(?:(?!" + LINK_PATTERN + r")[^·\n])*")
_VH_DESC_RE = re.compile(r"^description:\s*(.*)$")


def _volatile_signals(text: str) -> list:
    """Return the volatile-money signals present in a single string (see module
    comment). Currency is standalone; a magnitude token needs a money-context word."""
    signals: list[str] = []
    if _VH_CURRENCY_RE.search(text):
        signals.append("currency")
    if _VH_MAGNITUDE_RE.search(text) and _VH_MONEY_CTX_RE.search(text):
        signals.append("money-magnitude")
    return signals


def _extract_description(path) -> str:
    """Pull the frontmatter `description:` value from a memory file (single line).
    Returns '' when absent/unreadable. READS ONLY."""
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            head = [next(fh, "") for _ in range(20)]
    except OSError:
        return ""
    if not head or not head[0].startswith("---"):
        return ""
    for line in head[1:]:
        if line.strip() == "---":
            break
        m = _VH_DESC_RE.match(line)
        if m:
            return m.group(1).strip().strip('"').strip("'")
    return ""


def scan_volatile_hooks(memory_dir) -> dict:
    """Advisory: flag volatile-state values in MEMORY.md index hooks AND in each
    memory file's frontmatter `description:` (both are pointer-layer summaries that
    can go stale). READS ONLY; never mutates. High-precision money heuristic.

    IT HAS TWO CORPORA AND ONLY ONE OF THEM STILL EXISTS, so it counts both
    rather than printing one verdict over their union. The index half walks
    `[label](record.md)` pointers, and MEASURED 2026-09-20 on the operator's
    overlay there are ZERO of them: the conduct rules moved to
    `CLAUDE.private.md` and every per-record pointer went with them. So that
    half now reports clean over an empty set on every run, which is the vacuity
    `scripts/check-test-vacuity.py` refuses. The description half is untouched
    and still walks every fact file's frontmatter - 440 of them the same day -
    so it must not be removed along with the dead half. `pointers_scanned` and
    `descriptions_scanned` are what tell the two apart in the output.

    Returns:
        {
          "ok": bool,
          "flagged": [{"target": str, "line": str, "signals": [...]}, ...],
          "flagged_descriptions": [{"file": str, "description": str, "signals": [...]}, ...],
          "pointers_scanned": int,
          "descriptions_scanned": int,
          "note": str,
        }
    """
    memory_dir = Path(memory_dir)
    memory_file = memory_dir / "MEMORY.md"
    flagged: list[dict] = []
    pointers_scanned = 0
    if memory_file.exists():
        try:
            text = memory_file.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            return {
                "ok": False,
                "flagged": [],
                "flagged_descriptions": [],
                "pointers_scanned": 0,
                "descriptions_scanned": 0,
                "note": f"unreadable MEMORY.md: {exc}",
            }
        for raw in text.splitlines():
            if not raw.lstrip().startswith("-"):
                continue  # not an index bullet, so it names no memory to open
            # A hook runs from the end of the previous one to the end of its own
            # pointer, so it carries BOTH its leading label and its trailing
            # note: "- Mortgage EUR 412,000: [bank](x.md)" and
            # "- Mortgage: [bank](x.md) - EUR 412,000" are the same claim.
            #
            # This used to give the leading label to the FIRST pointer only
            # (`raw[:match.end()] if index == 0 else match.group(0)`). Every
            # later hook on a grouped line lost the words between the separator
            # and its own bracket, which is where the label lives, so a value
            # written "· costing EUR 412,000, see [rate](b.md)" was read by
            # nobody. Sliding the window makes every hook the same shape.
            prev_end = 0
            for match in _VH_POINTER_RE.finditer(raw):
                target = match.group("target")
                segment = raw[prev_end:match.end()]
                prev_end = match.end()
                if not target.endswith(".md"):
                    continue
                if target.startswith("threads/"):  # leak-guard: ok (relative prefix match on a MEMORY.md link target, not a path join)
                    continue
                pointers_scanned += 1
                signals = _volatile_signals(segment)
                if signals:
                    flagged.append({
                        "target": target,
                        "line": segment.strip(),
                        "signals": signals,
                    })

    flagged_desc: list[dict] = []
    descriptions_scanned = 0
    for p in sorted(memory_dir.glob("*.md")):
        if p.name == "MEMORY.md":
            continue
        desc = _extract_description(p)
        if not desc:
            continue
        descriptions_scanned += 1
        if signals := _volatile_signals(desc):
            flagged_desc.append({"file": p.name, "description": desc, "signals": signals})

    return {
        "ok": True,
        "flagged": flagged,
        "flagged_descriptions": flagged_desc,
        "pointers_scanned": pointers_scanned,
        "descriptions_scanned": descriptions_scanned,
        "note": (f"{len(flagged)} volatile hook(s) over {pointers_scanned} index "
                 f"pointer(s), {len(flagged_desc)} volatile description(s) over "
                 f"{descriptions_scanned} description(s)"),
    }


_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def scan_dangling_links(memory_dir) -> dict:
    """Advisory: find `[[wikilinks]]` in auto-memory that resolve to no memory file.

    A dangling link is cheap on its own -- the convention allows one as a marker
    for a memory worth writing later. It is expensive because the SENTENCE around
    it goes stale with it and nothing notices. On 2026-08-19 five files cited
    `no-exec-sync-until-ceo-cutover` as the reason work was "still deferred", two
    months after the deferral was lifted; the pointer being dead was the only
    outward sign that the premise was too.

    Two namespaces are deliberately not memory links and are skipped: `thread:*`
    addresses the thread registry, and an all-digit target is a bare record id.

    READS ONLY; never mutates. Returns:

        {
          "ok": bool,                 # False when the directory is unreadable
          "flagged": [{"target": str, "cited_by": [filename, ...]}, ...],
          "note": str,
        }
    """
    memory_dir = Path(memory_dir)
    if not memory_dir.is_dir():
        return {"ok": False, "flagged": [], "note": f"no such directory: {memory_dir}"}

    files = sorted(memory_dir.glob("*.md"))
    known = {p.stem for p in files}
    citers: dict[str, list[str]] = {}
    for p in files:
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            # A file we cannot read cannot be cleared, so say so by skipping it
            # rather than counting it as link-free.
            continue
        for target in _WIKILINK_RE.findall(text):
            if target in known or target.startswith("thread:") or target.isdigit():
                continue
            citers.setdefault(target, [])
            if p.name not in citers[target]:
                citers[target].append(p.name)

    flagged = [{"target": t, "cited_by": c} for t, c in sorted(citers.items())]
    return {"ok": True, "flagged": flagged, "note": f"{len(flagged)} dangling link(s)"}


# ---------------------------------------------------------------------------
# The second reachability source: the Hindsight bank `main`.
#
# Until 2026-10-04 this was a knowledge graph, read off its store file. The
# operator removed that graph from the workspace that day (HOS-198) and made
# Hindsight the main memory, in his words, translated: "Let Hindsight be the main
# memory" and "Reconfigure EVERYTHING onto Hindsight." So a record is reachable when
# MEMORY.md links it OR the bank holds it as a document, and an orphan is a
# record neither of them can reach.
#
# THE CONTRACT THIS READS, MEASURED 2026-10-04 against the live server with a
# read-only GET, and written down here because nothing else in this module
# would notice it moving (M039):
#   - `scripts/hindsight-sync.py` (`items_for`) sends each data-repository file
#     as one document whose `document_id` is its path relative to the data
#     root, so an auto-memory record arrives as `<auto-memory dir>/<name>.md`;
#     a file whose text is over `--max-bytes` arrives instead as
#     `<path>#part-NN`, one document per part.
#   - `GET /v1/default/banks/<bank>/documents?q=<text>&limit=&offset=` filters
#     by SUBSTRING of the id and returns `{"items", "total", "limit",
#     "offset"}`, each item carrying `id`. MEASURED: `q=auto-memory/` answered
#     635 of the bank's 6016 documents in 0.9 to 1.4 s, 374 KB; 631 ids began
#     with the auto-memory prefix, none was a part, none sat in a
#     subdirectory, and the other 4 carried the prefix in the MIDDLE of a
#     longer path, which is why the prefix is checked here rather than trusted
#     to the filter.
# A record sent in parts counts as held when ANY of its parts is listed: the
# question is whether the bank can return the record at all, and a partial
# upload is the sync's own business (`hindsight-sync.py status`).
#
# WHY A FILTERED LISTING AND NOT `hindsight-sync.list_documents()`: that walks
# the whole bank (6016 documents on the day above, 1000 a page) with a
# 120-second timeout per request fixed inside `_api`, and this module runs in
# `/prime`'s health panel at every session start. The settings (server, bank,
# key file) are NOT re-derived here: they come from `hindsight-sync.py`'s own
# `_conn()`, loaded by path because the script's name is hyphenated (the same
# loader `doc-versions.py` uses in `_hs()`); MEASURED 2026-10-04, that load
# costs 0.07 s. Only the GET is local, because the shared one cannot be given
# a short timeout.
# ---------------------------------------------------------------------------

#: Seconds one listing request may take before the bank counts as silent.
#: MEASURED 2026-10-04: the filtered auto-memory listing answered in 0.9 to
#: 1.4 s on an otherwise idle machine. Ten seconds is headroom for a busy
#: server, not a target, and it caps what a hung server costs a session start.
HINDSIGHT_TIMEOUT_S = 10.0

#: Ids per page. One page held the whole auto-memory listing on 2026-10-04.
HINDSIGHT_PAGE = 1000

#: A part suffix `hindsight-sync.py` appends to an id (`items_for`).
_PART_SUFFIX_RE = re.compile(r"#part-\d+$")


def _hindsight_conn() -> tuple[str, str, str]:
    """(server, bank, key) from `scripts/hindsight-sync.py`'s `_conn()`.

    Loaded by path from beside this checkout's `scripts/`, never from a
    constant, so a yard asks with its own copy of the settings code. Raises
    whatever the load or the key-file read raises; the caller turns that into
    "nothing answered".
    """
    import importlib.util
    import sys

    name = "_memory_health_hindsight_sync"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).resolve().parent.parent / "hindsight-sync.py")
        if spec is None or spec.loader is None:
            raise ImportError("scripts/hindsight-sync.py could not be loaded")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules[name] = module
    return sys.modules[name]._conn()


def _hindsight_get(url: str, key: str, timeout: float) -> dict:
    """One read-only GET against the Hindsight API, parsed as JSON.

    The seam the tests stub. GET only: this module never writes to the bank.
    """
    import json
    import urllib.request

    from scripts.utils.user_agent import user_agent

    if not url.startswith(("http://", "https://")):
        raise ValueError(f"not an http(s) URL: {url}")
    req = urllib.request.Request(  # noqa: S310 - scheme checked above
        url, method="GET",
        headers={"Authorization": f"Bearer {key}", "User-Agent": user_agent()})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - URL from the operator's settings
        return json.load(r)


def _auto_memory_prefix() -> str:
    """The id prefix auto-memory records carry in the bank, e.g. `auto-memory/`.

    The data-relative path of the registry name `auto-memory`, because that is
    exactly how `hindsight-sync.py` builds a `document_id`. Raises when the
    registry does not name it or it does not sit under the data root.
    """
    from scripts.utils.data_path import data_path
    from scripts.utils.paths import get_data_root

    rel = data_path("auto-memory").resolve().relative_to(get_data_root().resolve())
    return rel.as_posix().rstrip("/") + "/"


def hindsight_record_names(*, timeout: float = HINDSIGHT_TIMEOUT_S) -> set[str] | None:
    """Every auto-memory record NAME the Hindsight bank holds, or None.

    Names, not ids: `<prefix><name>.md` and `<prefix><name>.md#part-NN` both
    give `<name>.md`, and an id under a subdirectory of the prefix is dropped,
    because the corpus here is the flat `*.md` set of one directory. Matching by
    name is what lets `/prime`'s panel ask the same question of the harness's
    per-project copy of the memory directory, whose files carry the same names.

    **`None` MEANS THE BANK COULD NOT BE CONSULTED, AND IT IS NOT THE SAME
    ANSWER AS `set()`.** Carried over unchanged from the graph this replaced,
    and for the same reason. On 2026-09-20 the conduct rules left MEMORY.md and
    took every per-record link with them (MEASURED that day: 0 link targets
    beside 440 fact files), so the index proves reachability for nothing it
    does not link, and an empty set read as "the store holds nothing" convicts
    the whole corpus: MEASURED 2026-09-20 with no store on disk, 440 objective
    defects and exit 1, every one a record nobody had established anything
    about. A false alert believed is worse than none.

    So every way of not answering collapses to `None`, deliberately not told
    apart: settings that will not load, a key file that is absent, a refused or
    timed-out connection, a reply without `items` and `total`, a listing whose
    pages do not add up to its own `total` (a partial walk would under-report
    what the bank holds, which is the same false conviction by another route),
    and a bank that names no auto-memory record at all, which is a sync that
    never ran rather than a corpus of orphans. Whether what it names is ENOUGH
    to be believed is `hindsight_coverage`'s question, because only the caller
    knows the corpus.
    """
    try:
        api, bank, key = _hindsight_conn()
        prefix = _auto_memory_prefix()
    except Exception as exc:      # noqa: BLE001 - a health check must not die here
        logger.warning("Hindsight settings or the auto-memory address could not "
                       "be resolved (%s); reachability through the bank is "
                       "unknown, not empty", exc)
        return None
    import urllib.parse

    ids: list[str] = []
    total = None
    offset = 0
    try:
        while True:
            page = _hindsight_get(
                f"{api}/v1/default/banks/{urllib.parse.quote(bank, safe='')}/documents"
                f"?q={urllib.parse.quote(prefix, safe='')}"
                f"&limit={HINDSIGHT_PAGE}&offset={offset}",
                key, timeout)
            items, page_total = page.get("items"), page.get("total")
            if not isinstance(items, list) or not isinstance(page_total, int):
                logger.warning("the Hindsight listing carried no items or no "
                               "total; reachability through the bank is unknown")
                return None
            if total is None:
                total = page_total
            elif page_total != total:
                logger.warning("the Hindsight listing changed while paging "
                               "(total %d -> %d); reachability is unknown",
                               total, page_total)
                return None
            ids += [str(it.get("id") or "") for it in items if isinstance(it, dict)]
            offset += len(items)
            if not items or offset >= total:
                break
    except Exception as exc:      # noqa: BLE001 - a health check must not die here
        logger.warning("could not list the Hindsight bank (%s: %s); reachability "
                       "through the bank is unknown, and no record is reported "
                       "unreachable on the strength of a read that failed",
                       exc.__class__.__name__, exc)
        return None
    if len(ids) != total:
        logger.warning("the Hindsight listing returned %d of the %d ids it "
                       "reported; reachability is unknown rather than partial",
                       len(ids), total)
        return None
    names = set()
    for doc_id in ids:
        if not doc_id.startswith(prefix):
            continue
        rest = _PART_SUFFIX_RE.sub("", doc_id[len(prefix):])
        if rest and "/" not in rest:
            names.add(rest)
    if not names:
        logger.warning("the Hindsight bank named no auto-memory record; treating "
                       "reachability as unknown rather than reporting every "
                       "record unreachable")
        return None
    return names


# ---------------------------------------------------------------------------
# How much of the corpus the bank has to name before its silence means anything.
#
# THIS IS A POLICY NUMBER, NOT A MEASUREMENT. Decided 2026-09-20 in a yard, on
# HELM's recommendation, for the graph this replaced, while the operator was
# unreachable and had instructed the session to decide rather than wait; kept at
# the same value for the bank on 2026-10-04 (HOS-198), because the reasoning is
# about any store filled by a background pipeline, not about the graph it
# replaced. He can move it by editing this one line.
#
# The reasoning: below half the corpus, "this record is missing from the bank"
# is a statement about the SYNC and not about the record, and the gate's job is
# to report what has been established. Above it, a record the bank does not
# name is genuinely one the bank does not hold. MEASURED 2026-09-20 on the
# graph: a store 0.7% built (3 of 443) cleared a one-label test and the gate
# convicted 442 records; a sync that has only begun would do the same here.
#
# What was rejected, and why: an ABSOLUTE count (it does not scale with the
# corpus), and gating on the bank's mere reachability (that is the defect,
# restated). It is a TRIP POINT, never a target: a bank at 51% is not thereby
# healthy; `hindsight-sync.py status` and `plan` are what answer that.
HINDSIGHT_COVERAGE_FLOOR_FRACTION = 0.5


def hindsight_coverage(record_names, held,
                       *, floor_fraction: float = HINDSIGHT_COVERAGE_FLOOR_FRACTION) -> dict:
    """How much of the corpus the bank names, and whether that is an answer.

    Pure. `record_names` is the set of fact-file names (MEMORY.md excluded);
    `held` is what `hindsight_record_names()` returned, so `None` means nothing
    answered. Returns:

        {"named": int, "corpus": int, "floor": int,
         "floor_fraction": float, "answered": bool}

    `named` is the INTERSECTION with the corpus, so a name the bank holds for a
    record no longer on disk cannot raise the numerator.

    THE EMPTY CORPUS ANSWERS TRUE, deliberately: there is no record about which
    anything could be claimed, and `memory-hygiene.py` refuses outright (exit 2)
    when it reads zero fact files, before any verdict is printed.
    """
    record_names = set(record_names)
    corpus = len(record_names)
    floor = math.ceil(corpus * floor_fraction)
    named = len(record_names & set(held)) if held else 0
    return {
        "named": named,
        "corpus": corpus,
        "floor": floor,
        "floor_fraction": floor_fraction,
        "answered": held is not None and named >= floor,
    }


#: `in_hindsight` was not passed, so the bank is consulted for real. A sentinel
#: and not `None`, because `None` is a MEANING here ("nothing answered") and a
#: caller must be able to say it deliberately.
_HINDSIGHT_UNSET = object()


def compute_memory_defects(memory_dir: Path, *, in_hindsight=_HINDSIGHT_UNSET) -> dict:
    """Compute objective auto-memory defects for a single memory directory.

    `in_hindsight` is the second reachability source, and it is a PARAMETER (as
    its predecessor `in_graph` was from 2026-09-20) so the function stays pure
    when the caller already holds the answer. Pass a set of record names to say
    the bank answered with them, `None` to say nothing answered, or leave it
    out and the bank is asked through `hindsight_record_names()`.

    **THE COVERAGE FLOOR APPLIES TO THE BANK THIS FUNCTION ASKED, AND NOT TO AN
    ANSWER THE CALLER STATED**, and the asymmetry is the parameter's meaning
    rather than an exemption. `hindsight_record_names()` returns a raw name
    set, a PRE-decision reading that still has to be judged an answer or not,
    and `hindsight_coverage` is that judgement. `in_hindsight=` is the
    POST-decision answer, supplied by a caller who has already made it, and
    running the floor over it would overrule the caller on the one question the
    parameter exists to let it settle. Said out loud because it is a real
    limit: passing `in_hindsight=set()` over a non-empty corpus stipulates a
    state the live sync cannot reach, so such a call exercises the INDEX half
    of the orphan rule and establishes nothing about the bank half.

    Why the source is a parameter at all: MEASURED 2026-09-20, twenty tests
    across five files asserted an orphan list while the store of that day was
    not consulted, and passed because an absent store read as "holds nothing".
    Making an unanswered store honest turned all twenty red at once.

    Returns a pure data dict (no human-facing string, no exit code). Callers
    decide how to present it and which subset gates. Shape:

        {
          "status": "ok" | "missing",
          "memory_dir": str,
          "file_count": int,            # *.md files excluding nothing (incl. MEMORY.md)
          "memory_md_lines": int,       # line count of MEMORY.md (0 if absent)
          "over_budget": bool,          # memory_md_lines > MEMORY_BUDGET_LINES
          "memory_md_utf16_units": int, # MEMORY.md size in UTF-16 code units
          "over_byte_budget": bool,     # units > MEMORY_INDEX_UTF16_BUDGET
          "near_byte_budget": bool,     # not over, but past the warn fraction
          "stale": list[tuple[str,int]],# [(filename, days_old), ...] for >STALE_DAYS
          "orphans": list[str],         # reachable through neither source; [] when
                                        # `orphans_evaluated` is False
          "orphans_evaluated": bool,    # the bank answered, so a record it does
                                        # not hold is genuinely unreachable
          "hindsight_named_records": int,  # corpus records the bank names
          "hindsight_corpus_records": int, # fact files on disk (MEMORY.md excluded)
          "hindsight_coverage_floor": int, # records it had to name to count as an answer
          "index_readable": bool,       # MEMORY.md exists and could be read
          "index_problem": str,         # why not, "" when it could
        }

    THE THREE HINDSIGHT KEYS ARE RETURNED WHICHEVER WAY THE BRANCH WENT, so a
    caller can print how complete the bank is instead of inferring it. Until
    2026-09-20 the only way to discover that the store of that day held 3 of 443
    records was to read the defect count and work backwards, and a gauge that
    makes you deduce its own reliability is one nobody checks.

    THE TWO BUDGET AXES ARE NOT THE SAME QUESTION, and until 2026-09-09 only the
    line one was asked. MEASURED that day on the operator's index: 125 of 200
    lines (0.63) and 22732 of 25000 UTF-16 units (0.91). This function reported
    `over_budget: False` and was right; the axis about to truncate the file was
    measured by nothing. See `MEMORY_INDEX_UTF16_BUDGET` for what the harness
    does at the ceiling, which is to cut silently rather than refuse.

    `memory_md_lines` keeps its own arithmetic (physical lines of the file as
    read) rather than adopting the harness's `trimmed newlines + 1`. The two
    agree on any index that ends in a single newline, and the existing key has 26
    callers; changing what it counts to gain agreement in the trailing-blank-line
    case would be a silent change of meaning under all of them. The byte axis is
    new, so it is measured the harness's way from the start.

    BOTH branches return the same keys, so `result["index_readable"]` never
    raises on the path where the index is certainly unreadable (it did on
    2026-08-30, when the `"missing"` branch returned seven).
    """
    if not memory_dir.is_dir():
        return {
            "status": "missing",
            "memory_dir": str(memory_dir),
            "file_count": 0,
            "memory_md_lines": 0,
            "over_budget": False,
            "memory_md_utf16_units": 0,
            "over_byte_budget": False,
            "near_byte_budget": False,
            "stale": [],
            "orphans": [],
            "orphans_evaluated": False,
            "hindsight_named_records": 0,
            "hindsight_corpus_records": 0,
            "hindsight_coverage_floor": 0,
            "index_readable": False,
            "index_problem": f"{memory_dir} is not a directory",
        }

    files = sorted(p for p in memory_dir.glob("*.md") if p.is_file())
    memory_file = memory_dir / "MEMORY.md"

    if memory_file.exists():
        try:
            # `errors="ignore"`, like the orphan read below: a strict read raised
            # `UnicodeDecodeError` (a ValueError, not OSError) on one bad byte
            # (2026-09-01). An invalid byte cannot change a LINE count.
            lines = sum(1 for _ in memory_file.open(
                "r", encoding="utf-8", errors="ignore"))
        except OSError:
            lines = 0
    else:
        lines = 0

    # Stale: mtime older than STALE_DAYS (tz-aware local time via get_default_tz).
    now = datetime.datetime.now(get_default_tz())
    stale: list[tuple[str, int]] = []
    for p in files:
        if p.name == "MEMORY.md":
            continue
        try:
            mtime = datetime.datetime.fromtimestamp(p.stat().st_mtime, tz=get_default_tz())
        except OSError:
            continue
        age = (now - mtime).days
        if age > STALE_DAYS:
            stale.append((p.name, age))

    # Orphans: fact files the index carries no pointer to.
    #
    # A record is an orphan only when the index does not link it (exact
    # `](<name>)` targets via `memory_expiry.index_link_targets`, the index's
    # one link grammar) AND the Hindsight bank `main` does not hold it
    # (`hindsight_record_names()`, asked of the bank, not of what is eligible to
    # send). Since 2026-09-20 the index links no records, so the bank is the only
    # source that can say no. When the bank cannot be consulted nothing is
    # decided: the list is empty and `orphans_evaluated` is False beside it,
    # never a corpus-sized false alarm. `index_readable` says whether the index
    # was actually read.
    orphans: list[str] = []
    index_readable = True
    index_problem = ""
    content = ""
    if not memory_file.exists():
        index_readable = False
        index_problem = f"{memory_file} does not exist"
    else:
        try:
            content = memory_file.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            index_readable = False
            index_problem = f"{memory_file} could not be read: {exc}"

    # AND SINCE 2026-09-20, SECOND PASS: "the store answered" is a question
    # about how much of the corpus it names, not about whether it named
    # anything. The first repair that day drew the line at one name and a
    # background pipeline walked straight over it, so the gate convicted 442 of
    # 443 records on the strength of a store that was 0.7% built. See
    # `HINDSIGHT_COVERAGE_FLOOR_FRACTION` for the policy and what was rejected.
    linked = index_link_targets(content)
    record_names = {p.name for p in files if p.name != "MEMORY.md"}
    bank_asked = in_hindsight is _HINDSIGHT_UNSET
    if bank_asked:
        in_hindsight = hindsight_record_names()
    coverage = hindsight_coverage(record_names, in_hindsight)
    if bank_asked and in_hindsight is not None and not coverage["answered"]:
        logger.warning(
            "the Hindsight bank names %d of %d record(s), under the floor of %d "
            "(%.0f%% of the corpus); treating reachability as unknown rather "
            "than reporting %d record(s) unreachable on the strength of a "
            "part-synced bank",
            coverage["named"], coverage["corpus"], coverage["floor"],
            100 * coverage["floor_fraction"],
            coverage["corpus"] - coverage["named"])
        in_hindsight = None
    bank_consulted = in_hindsight is not None
    if bank_consulted:
        for p in files:
            if p.name == "MEMORY.md":
                continue
            if p.name not in linked and p.name not in in_hindsight:
                orphans.append(p.name)

    # The byte axis comes off `content`, the read taken above for the orphan
    # scan, so this adds no third open of the same file. It inherits that read's
    # failure shape too: an unreadable index leaves `content` empty, so the unit
    # count is 0 and `index_readable` is False beside it - the same pair of
    # facts the line count already hands the caller, and the reason
    # `memory-hygiene.py` treats an unread index as a budget it did not evaluate
    # rather than a budget it cleared.
    _, units = measure_index(content)
    over_bytes = units > MEMORY_INDEX_UTF16_BUDGET

    return {
        "status": "ok",
        "memory_dir": str(memory_dir),
        "file_count": len(files),
        "memory_md_lines": lines,
        "over_budget": lines > MEMORY_BUDGET_LINES,
        "memory_md_utf16_units": units,
        "over_byte_budget": over_bytes,
        "near_byte_budget": (
            not over_bytes
            and units >= MEMORY_INDEX_UTF16_BUDGET * MEMORY_INDEX_WARN_FRACTION
        ),
        "stale": stale,
        "orphans": orphans,
        "orphans_evaluated": bank_consulted,
        "hindsight_named_records": coverage["named"],
        "hindsight_corpus_records": coverage["corpus"],
        "hindsight_coverage_floor": coverage["floor"],
        "index_readable": index_readable,
        "index_problem": index_problem,
    }


def _cosine(u, v):
    # `strict=False` deliberately: two vectors of different length mean the
    # embedder returned something unexpected, and a raise here would take down an
    # ADVISORY scan whose whole contract is to degrade. The score is meaningless
    # in that case either way, and `scan_redundancy` reports it as a pair below
    # threshold rather than as a crash.
    dot = sum(a * b for a, b in zip(u, v, strict=False))
    nu = math.sqrt(sum(a * a for a in u))
    nv = math.sqrt(sum(b * b for b in v))
    return 0.0 if nu == 0 or nv == 0 else dot / (nu * nv)


def scan_redundancy(memory_dir, *, threshold=0.86, embedder=None, timeout=120) -> dict:
    """Advisory near-duplicate detector over the `auto-memory` records. Proposes only; never
    mutates. Returns {"ok": bool, "pairs": [{a,b,score}], "note": str}. Degrades to
    ok=False (never raises) when the embedder is unavailable.

    `timeout` (seconds, default 120) is the per-request socket timeout passed
    to the default embedder. A single request can batch up to 32 full memory
    files -- on CPU-only ollama that can exceed 120s as the corpus grows, so a
    background/cron caller with no interactive latency pressure (e.g.
    dream-shadow.py) should pass a longer value. Ignored when a custom
    `embedder` callable is supplied (the caller owns its own timeout then).

    The corpus is READ before the embedder is resolved, so an unreadable memory
    file is named and counted even on a run that has no embedder at all. See the
    comment at the walk for the measurement behind that order."""
    files = sorted(p for p in Path(memory_dir).glob("*.md") if p.name != "MEMORY.md")
    if len(files) < 2:
        return {"ok": True, "pairs": [], "note": "fewer than 2 memory files"}
    # The corpus is READ BEFORE the embedder is resolved: a pinned embedder that
    # is down refuses, and an outage must not hide an unreadable memory file
    # (2026-09-03). An unreadable file is dropped from the pair scan, with
    # `files` kept in step with `texts`, and the note counts it (2026-08-30).
    readable, texts, unreadable = [], [], 0
    for f in files:
        try:
            texts.append(f.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            # `UnicodeDecodeError` is a ValueError, not OSError (2026-09-01).
            unreadable += 1
            logger.warning("dropping unreadable memory file %s from the "
                           "redundancy scan", f, exc_info=True)
            continue
        readable.append(f)
    files = readable
    skipped = f"; {unreadable} unreadable file(s) skipped" if unreadable else ""
    if len(files) < 2:
        return {"ok": False, "pairs": [],
                "note": f"fewer than 2 readable memory files ({unreadable} unreadable)"}
    if embedder is None:
        try:
            from scripts.utils.embeddings import embed, index_embed_target

            host, model = index_embed_target()

            def embedder(ts):
                return embed(ts, model=model, host=host, timeout=timeout)
        except Exception as e:
            return {"ok": False, "pairs": [],
                    "note": f"embedder unavailable: {e}{skipped}"}
    try:
        vecs = embedder(texts)
    except Exception as e:
        return {"ok": False, "pairs": [],
                "note": f"embedder unavailable: {e}{skipped}"}
    pairs = []
    for i in range(len(files)):
        for j in range(i + 1, len(files)):
            cos = _cosine(vecs[i], vecs[j])
            if cos >= threshold:
                pairs.append({"a": files[i].name, "b": files[j].name, "score": round(cos, 4)})
    pairs.sort(key=lambda p: p["score"], reverse=True)
    return {"ok": True, "pairs": pairs,
            "note": f"{len(pairs)} near-duplicate pair(s){skipped}"}
