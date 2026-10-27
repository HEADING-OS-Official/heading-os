#!/usr/bin/env python3
"""Odin cadence checker -- read-only proactive nudge for /odin collect + reflect.

Computes whether harvestable episodes or mature-able episode clusters have
accumulated since the last collect, and emits a one-line reminder. SURFACES
COUNTS ONLY, NEVER CONTENT. Never drafts an episode, never calls the LLM, never
runs collect/reflect, never writes to the brain. The CEO's per-candidate gate in
`/odin collect` stays the only path to a brain write -- this script only suggests.

Usage:
    python3 scripts/odin-cadence.py                 # one-line suggestion (or "up to date")
    python3 scripts/odin-cadence.py --json          # machine-readable
    python3 scripts/odin-cadence.py --quiet         # print nothing unless a nudge is due
    python3 scripts/odin-cadence.py --min-entries 8 # override the un-harvested threshold

Three signals (all read-only, counts only):
  (a) days_since_collect  -- from `odin-brain`'s .last-collect (absent = never).
  (b) unharvested_total   -- dated entries ON OR AFTER the marker date across
                             collect's EXACT allowlist, using the SAME deny
                             predicate + assistant-inbox gate. That predicate withholds
                             nothing here since 2026-09-12: this script passes no
                             deny lists, and the unconditional ones were removed
                             from scripts/utils/air_gap.py that day. What keeps a
                             personal thread out of this count is the ALLOWLIST
                             (the `threads-business` glob), which is a glob, not a gate. The boundary is inclusive because the
                             marker carries a date and no time, so an entry
                             logged later on the day of the collect cannot be
                             told from one the collect already saw; counting it
                             again is the direction that cannot lose material.
                             The reflect side below is deliberately STRICT (`>`)
                             and says why: the two boundaries differ on purpose.
  (c) reflect_clusters    -- connected components (size >= 2) of raw-status episodes
                             sharing >= CLUSTER_MIN_SHARED tags, counted only where
                             they hold an episode logged after `.last-reflect`. A
                             cluster whose oldest unreviewed episode has waited
                             >= STALE_CLUSTER_DAYS escalates as "stale", so material
                             you keep passing over surfaces distinctly from material
                             that arrived yesterday.

Both markers are version-controlled in the data overlay, so every clone answers
the same question the same way (an untracked marker reads as `null` and counts
every entry as un-harvested). They hold one ISO date each and no content.

The counted source set MUST equal /odin collect's allowlist (mode-catalog.md):
the business thread records (the `business-threads` group: `threads-business`
and every account's `account-threads`), the `crm-contacts` records (excluding
.migration-backup/ + aggregated/), and the `assistant-inbox-state` record -- same globs,
same exclusions, same gate; resolve a name with
`python scripts/data-path.py path <name>`.
If mode-catalog's allowlist or detection regexes change, change this script too.

Needs no ollama (pure counting). Exit 0 on every COUNTING outcome --
including "nothing due", which is not a failure. An OSError from the
filesystem (permissions, a full disk, a dropped mount) still propagates
and exits non-zero: that is not a cadence verdict and must not be
reported as one (the notifier would read it as "up to date").

Gap #5 enrichment: when reflect-ready clusters exist, `main()` also writes a
dated report (episode membership + shared tags per cluster) into the
`odin-cadence-reports` records -- never to the brain. This is the ONE
side effect this otherwise read-only script has; `compute()` itself performs
no writes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

# Workspace import bootstrap (per development-standards.md)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.markdown import FM_OK, split_frontmatter  # noqa: E402
from scripts.utils.data_path import DataPathError, data_path  # noqa: E402
from scripts.utils.workspace import get_data_root, get_default_tz  # noqa: E402
from scripts.utils.air_gap import is_denied  # noqa: E402
from scripts.utils import assistant_inbox  # noqa: E402

# ============================================================
# Configuration
# ============================================================

DAYS_THRESHOLD = 7          # nudge if collect last ran >= this many days ago
DEFAULT_MIN_ENTRIES = 5     # nudge if un-harvested entries reach this count
STALE_CLUSTER_DAYS = 14     # unreviewed cluster material waiting this long escalates as "stale"

# Shared tags required to join two raw episodes into one reflect cluster.
#
# It was 1 until 2026-08-11, and 1 does not survive contact with a real brain: a
# single shared tag is a topic coincidence, and with union-find the coincidences
# chain. Measured over the 15 raw episodes standing that day, a threshold of 1
# produced ONE component of 12 — a set of deal episodes welded to a set of
# internal-tooling episodes through the workspace's own organisation tag, carried
# by 6 of the 15, and then through generic domain labels such as `go-to-market`,
# `ownership` and `governance`. Those two themes have nothing to do with each
# other, so "1 cluster" told the reflect pass nothing it could act on.
#
# The alternatives were measured on the same set. Dropping high-frequency tags
# instead is worse: the cutoff that breaks the weld also discards the slug of a
# recurring counterpart, and a counterpart who keeps reappearing is the most
# meaningful clustering signal there is. A threshold of 2 still merged the two
# themes (via `go-to-market`+`positioning` and `governance`+`tooling`). Three
# independently agreeing tags is the point where the components matched what a
# human would call a theme: two clusters of three, one per theme.
#
# The failure direction is deliberate. Under-clustering delays a reflect pass;
# over-clustering produces a nudge that is always on, which costs the signal.
CLUSTER_MIN_SHARED = 3

# Names INSIDE the Odin brain, whose own address is the registry's `odin-brain`
# (`_odin_brain`). Until 2026-09-25 each carried the brain's address written
# out in front of it; HOS-5 section 6 took the address back to the registry.
MARKER = ".last-collect"
REFLECT_MARKER = ".last-reflect"
EPISODES_DIR = "episodes"


def _odin_brain(root: Path) -> Path:
    """The Odin brain under `root`, by its registry name. Raises on a registry
    that does not name it: a brain nobody can locate is not an empty brain."""
    return data_path("odin-brain", data_root=root)

# Floor used when no marker exists: count everything (a "never collected" state
# still nudges regardless of count, but the figures stay meaningful).
EPOCH_FLOOR = "0001-01-01"

# Thread sections collect harvests from (prefix match -- real headers carry
# suffixes like "## Log (newest first)").
THREAD_SECTIONS = ("Log", "Recent activity", "Decisions")

# The prefix match needs a word boundary after it. A bare `head.startswith(s)`
# also accepted `## Logistics`, `## Login attempts` and `## Logic`, whose dated
# bullets were then counted as harvestable -- while the module docstring above
# promises the counted set EQUALS /odin collect's allowlist, "same globs, same
# exclusions". Measured on a two-section fixture, `## Logistics` plus
# `## Login attempts` contributed 2 phantom un-harvested entries. The suffix the
# comment above licenses (" (newest first)") opens with a space, so requiring
# the next character to be a non-word one keeps every real header and drops the
# coincidental ones.
THREAD_SECTION_RE = re.compile(
    r"^(?:%s)(?![0-9A-Za-z_])" % "|".join(re.escape(s) for s in THREAD_SECTIONS)
)

# The SAME two dated-entry forms collect uses (separator spans em/en-dash + hyphen).
THREAD_HEADING_RE = re.compile(r"^###\s+(\d{4}-\d{2}-\d{2})\s*[—–-]\s*.+$")
THREAD_BULLET_RE = re.compile(r"^-\s+(\d{4}-\d{2}-\d{2})\s*[—–-]\s*.+$")
SECTION_RE = re.compile(r"^##\s+(.+?)\s*$")

# The SAME CRM interaction-log row collect uses.
CRM_ROW_RE = re.compile(
    r"^### (?P<date>\d{4}-\d{2}-\d{2})( \d{2}:\d{2})? \| [^|]+ \| .+$"
)

# Allowlist exclusions (belt-and-braces; these dirs are not under `crm-contacts`).
CRM_EXCLUDE = ("/.migration-backup/", "/aggregated/")


# ============================================================
# Marker
# ============================================================

def read_reflect_marker(root: Path):
    """Return the ISO date of the last CEO-confirmed reflect pass, or None.

    `.last-reflect` is written by `/odin reflect` on a maturation pass, exactly as
    `.last-collect` is written by `/odin collect`. Until 2026-08-11 nothing read
    it, so the cluster signal below could not tell reviewed material from new.
    """
    p = _odin_brain(root) / REFLECT_MARKER
    if not p.exists():
        return None
    # Same shape as `read_marker` below, and fixed in the same change rather
    # than one at a time: an unreadable reflect marker returns None, which the
    # caller already treats as "no confirmed reflect pass" and is the safe
    # direction (it makes the cluster signal treat material as UNREVIEWED).
    try:
        raw = p.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        date.fromisoformat(raw[:10])
    except ValueError:
        return None
    return raw[:10]


def read_marker(root: Path):
    """Return (marker_str|None, days_since|None). Absent marker -> (None, None)."""
    p = _odin_brain(root) / MARKER
    if not p.exists():
        return None, None
    # `errors="replace"` closed the DECODE half of this; the read could still
    # raise an OSError. MEASURED 2026-09-01 with the marker at mode 000:
    # `PermissionError` out of this function, and out of `marker_state` below,
    # whose docstring undertakes to RETURN "unreadable" for exactly this case.
    # `(None, None)` is the same safe direction the corrupt-marker branch below
    # already chose: it falls the caller through to `EPOCH_FLOOR` and counts
    # everything, which is the right way to be wrong about a marker.
    try:
        raw = p.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None, None
    if not raw:
        return None, None
    try:
        d = date.fromisoformat(raw[:10])
    except ValueError:
        # A corrupt marker returned as TRUTHY made `compute` use the garbage
        # string as its lexicographic `since` floor, so `"2026-..." >= "garbage"`
        # was False for every entry and all un-harvested counts silently read 0
        # -- while the reason said "never collected" beside a marker file that
        # plainly exists. None here falls the caller through to EPOCH_FLOOR,
        # which counts everything: the safe direction for an unreadable marker.
        return None, None
    return raw, (datetime.now(get_default_tz()).date() - d).days


def marker_state(root: Path) -> str:
    """"absent" | "ok" | "unreadable" for `.last-collect`.

    `read_marker` collapses absent and corrupt into the same `(None, None)`, and
    that collapse is right for the COUNTING direction: both fall through to
    `EPOCH_FLOOR` and count everything, which is the safe way to be wrong. It is
    wrong for the SENTENCE. A garbage marker made the JSON say
    `"last_collect": null` and the nudge -- sent verbatim to Telegram -- say
    "collect never run", beside a marker file that plainly exists, and nothing
    anywhere said the file was unreadable. A torn write therefore masqueraded as
    a fresh install indefinitely.

    An existing but EMPTY marker is unreadable, not absent, for the same reason:
    something wrote that file.
    """
    p = _odin_brain(root) / MARKER
    if not p.exists():
        return "absent"
    # A file that exists and will not open IS the "unreadable" this function is
    # named for, and until 2026-09-01 it was the one input that raised instead.
    # MEASURED that day at mode 000: `PermissionError` out of a function whose
    # first docstring line is the enumeration `"absent" | "ok" | "unreadable"`.
    # Returning the value the contract already names costs nothing and is what
    # the two branches below were written to express.
    try:
        raw = p.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return "unreadable"
    if not raw:
        return "unreadable"
    try:
        date.fromisoformat(raw[:10])
    except ValueError:
        return "unreadable"
    return "ok"


# ============================================================
# Frontmatter (lightweight -- scalar + simple list fields only)
# ============================================================

def _frontmatter_block(text: str) -> str:
    """The raw YAML block, or "" when the document has no usable frontmatter.

    Fences via the shared splitter since 2026-08-28. `startswith("---")` plus
    `find("\\n---", 3)` tested for the CHARACTERS: MEASURED against the shared
    splitter over eight documents, a fence written `---\\t` and an opening line
    `---extra` both left the malformed opener inside the block, and PyYAML then
    failed on a file whose YAML is fine. `_fm_scalar` and `_fm_list` below read
    this block with their own line regexes, which is why only the fences move.
    """
    block, _body, kind = split_frontmatter(text)
    return block if (block is not None and kind == FM_OK) else ""


def _strip_comment(raw: str) -> str:
    """Drop a trailing YAML comment: an unquoted `#` preceded by whitespace.

    `odin-brain-health.py` reads these same files through `yaml.safe_load`,
    which honours comments; this hand-rolled parser did not, so the two tools
    disagreed about one file on disk. Measured: `status: raw  # draft` came back
    as the string `"raw  # draft"`, compared unequal to `"raw"`, and the episode
    dropped out of clustering entirely -- one cluster became zero, with nothing
    appended to `compute()`'s `skipped` list, so the JSON asserted a complete
    pass it had not made. `keywords: [a, b]  # note` and `keywords:  # note`
    both defeated `_fm_list`'s regexes the same silent way.

    A `#` inside quotes, and one with no whitespace in front of it (`#fff`), is
    part of the value and is left alone -- that is YAML's own rule.
    """
    out = []
    quote = None
    for i, ch in enumerate(raw):
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            continue
        if ch == "#" and i and raw[i - 1] in " \t":
            break
        out.append(ch)
    return "".join(out).rstrip()


def _fm_scalar(block: str, key: str) -> str:
    m = re.search(rf"^{re.escape(key)}:\s*(.*)$", block, re.MULTILINE)
    if not m:
        return ""
    return _strip_comment(m.group(1)).strip().strip('"').strip("'")


def _fm_list(block: str, key: str):
    """Parse a frontmatter list field in EITHER YAML form.

    Inline, `key: [a, b, c]`, or a block list::

        key:
          - a
          - b

    Only the inline form was matched. `odin-brain-health.py` reads both (it goes
    through `yaml.safe_load` and its docstring says so), so the two tools
    disagreed about the same file: a block-form episode came back with an EMPTY
    tag set, joined no cluster, and was still counted as scanned -- `compute()`
    appends nothing to its `skipped` list for this case, so the JSON asserted a
    complete pass it had not made.

    Latent for episodes today (90 of 90 use the inline form), not hypothetical
    for the brain: block-list frontmatter is already on disk under `sources/`.
    """
    # Comment-stripped per line, indentation preserved (the block-list branch
    # below anchors its key at column 0 and reads the indented `- ` items).
    lines = [_strip_comment(ln) for ln in block.splitlines()]
    block = "\n".join(lines)
    m = re.search(rf"^{re.escape(key)}:\s*\[(.*)\]\s*$", block, re.MULTILINE)
    if m:
        inner = m.group(1).strip()
        parts = inner.split(",") if inner else []
    else:
        key_re = re.compile(rf"^{re.escape(key)}:[ \t]*$")
        parts = None
        for idx, line in enumerate(lines):
            if key_re.match(line):
                parts = []
                for nxt in lines[idx + 1:]:
                    stripped = nxt.lstrip()
                    if not stripped.startswith("- "):
                        break
                    parts.append(stripped[2:])
                break
        if parts is None:
            return []
    out = []
    for part in parts:
        v = part.strip().strip('"').strip("'").strip()
        if v:
            out.append(v.lower())
    return out


# ============================================================
# (b) Un-harvested entry counts
# ============================================================

def count_threads(root: Path, since: str, skipped: list) -> int:
    """Count dated entries (date >= since) in allowed sections of business threads.

    `skipped` is required, the same list `count_assistant_inbox` takes: the zeros that
    are NOT counts (no address, no directory, no `.md` in it) are named there
    instead of being rendered as a quiet 0 that reads as "nothing to harvest".
    """
    n = 0
    # A tree whose registry lacks the name refuses into `skipped`, never a
    # traceback and never a quiet 0. `data_root=root` honours the caller's root
    # (`main()` passes `get_data_root()`).
    try:
        base = data_path("threads-business", data_root=root)
        # Every account's threads as well (HOS-234 part 10): the C3 move takes
        # them out of `threads-business`, and they are business threads.
        from scripts.utils.threads_lib import business_thread_dirs
        homes = business_thread_dirs(data_root=root)
    except DataPathError as exc:
        skipped.append(f"threads: no business thread address on this tree ({exc})")
        return 0
    if not homes:
        skipped.append(f"threads: no business thread directory at {base}")
        return 0
    cards = sorted(p for h in homes for p in h.glob("*.md"))
    if not cards:
        skipped.append(f"threads: {', '.join(str(h) for h in homes)} hold no .md file")
        return 0
    for p in cards:
        rel = p.relative_to(root).as_posix()
        if is_denied(rel):
            continue
        # A walker, so one unreadable thread must not end the count. Skipping
        # is NAMED: a silently dropped thread turns this count into a lower
        # bound that the caller reads as a total, and the count drives a nudge
        # sent verbatim to Telegram.
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"[warn] odin-cadence: skipping unreadable thread {p}: {exc}",
                  file=sys.stderr)
            continue
        block = _frontmatter_block(text)
        # Frontmatter guard: business + ceo-only only.
        if _fm_scalar(block, "type") != "business":
            continue
        if _fm_scalar(block, "classification") != "ceo-only":
            continue
        in_section = False
        for line in text.splitlines():
            sec = SECTION_RE.match(line)
            if sec:
                head = sec.group(1)
                in_section = bool(THREAD_SECTION_RE.match(head))
                continue
            if not in_section:
                continue
            m = THREAD_HEADING_RE.match(line) or THREAD_BULLET_RE.match(line)
            if m and m.group(1) >= since:
                n += 1
    return n


def count_crm(root: Path, since: str, skipped: list) -> int:
    """Count CRM interaction-log rows (date >= since) across the `crm-contacts` records.

    Same two silences as `count_threads`, closed the same way and on the same
    day. MEASURED 2026-09-18: the pre-move CRM contacts directory was vacated
    by the move and the `crm-contacts` address held 195 cards, so this returned
    0 over the whole CRM.

    The address comes from `scripts.utils.assistant_inbox.contacts_home`
    rather than being joined here, because `count_assistant_inbox` below already builds
    its vocabulary from that same directory through that same helper, and two
    counters on one run disagreeing about where the CRM is would be worse than
    either being wrong.
    """
    n = 0
    base = assistant_inbox.contacts_home(root)
    if not base.is_dir():
        skipped.append(f"crm: no contacts directory at {base}")
        return 0
    cards = sorted(base.glob("*.md"))
    if not cards:
        skipped.append(f"crm: {base} holds no .md file")
        return 0
    for p in cards:
        rel = p.relative_to(root).as_posix()
        if is_denied(rel) or any(x in f"/{rel}" for x in CRM_EXCLUDE):
            continue
        # Same walker rule as `count_threads`: skip the card, name it, keep
        # counting. This total is quoted to the operator, so a card dropped in
        # silence turns it into a lower bound that reads as a total.
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"[warn] odin-cadence: skipping unreadable CRM card {p}: "
                  f"{exc}", file=sys.stderr)
            continue
        for line in text.splitlines():
            m = CRM_ROW_RE.match(line)
            if m and m.group("date") >= since:
                n += 1
    return n


def count_assistant_inbox(root: Path, since: str, skipped: list) -> int:
    """Count assistant inbox messages admitted by the SAME counterpart gate (date >= since).

    The address comes from `assistant_inbox.state_path`, which owns it, so
    the gate module and this counter cannot end up reading two different files.
    """
    state_path = assistant_inbox.state_path(root)
    if not state_path.exists():
        skipped.append("assistant inbox: state.json absent")
        return 0
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # `ValueError`, not `json.JSONDecodeError`. `read_text(encoding="utf-8")`
        # raises `UnicodeDecodeError` on non-UTF-8 bytes, a SIBLING of
        # JSONDecodeError under ValueError rather than a subclass, so a state
        # file that had been torn mid-write escaped this handler entirely.
        # MEASURED 2026-09-01: `compute()` died rather than appending the
        # "state.json unreadable" line to `skipped`, which is the whole point of
        # the `skipped` list - the JSON must never assert a complete pass it did
        # not make.
        skipped.append(f"assistant inbox: state.json unreadable ({type(exc).__name__})")
        return 0
    # A gate that cannot classify anything is not a count of zero. `build_vocab`
    # refuses since 2026-09-18 rather than returning an empty dict, and the
    # refusal is reported here rather than crashing the cadence: this function's
    # contract with `compute` is a number plus an honest `skipped`, and a nudge
    # that dies takes the thread and CRM counts down with it.
    try:
        vocab = assistant_inbox.build_vocab(root, skipped)
    except assistant_inbox.VocabularyEmpty as exc:
        skipped.append(f"assistant inbox: counterpart vocabulary refused ({exc})")
        return 0
    n = 0
    for _mid, msg in data.get("messages", {}).items():
        admit, _reason, _r = assistant_inbox.gate_message(msg, vocab, since)
        if admit:
            n += 1
    return n


def assistant_inbox_source_state(root: Path) -> dict:
    """What the assistant inbox store HOLDS, so that an `assistant inbox: 0` can be read.

    `count_assistant_inbox` answers one question: how many messages are admissible since
    the marker. It returns a bare 0 for three different worlds. Every newer
    message was gated out; no message is newer than the marker; and the source
    has produced nothing at all for weeks. The first two are the cadence's
    business and the third is an outage in a feed nobody watches, and all three
    looked identical, because `skipped` only records a store this code could not
    READ.

    MEASURED 2026-09-06 against the live overlay: 77 messages in the store, the
    newest dated 2026-07-11, a month BEFORE the 2026-08-11 collect marker.
    Reported as `assistant inbox: 0` with an empty `skipped`, which is arithmetically
    correct and says the opposite of what the operator would conclude from it.

    Returns ``{"messages": int|None, "newest": str|None}``. None means the store
    could not be read or parsed; `count_assistant_inbox` already names that in `skipped`,
    and this function deliberately does not add a second copy of the complaint.
    An empty store is ``{"messages": 0, "newest": None}``, which is not the same
    answer and must not be collapsed into it.
    """
    state_path = assistant_inbox.state_path(root)
    if not state_path.exists():
        return {"messages": None, "newest": None}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Same two-clause shape, and the same reason, as `count_assistant_inbox` above:
        # a torn write surfaces as UnicodeDecodeError, a ValueError sibling of
        # JSONDecodeError rather than a subclass. Swallowed rather than logged
        # because the caller records it once already.
        return {"messages": None, "newest": None}
    messages = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(messages, dict):
        # Valid JSON of the wrong shape. `count_assistant_inbox` reaches the same state
        # through `data.get("messages", {})` returning a non-dict and dying on
        # `.items()`; this one refuses to guess instead.
        return {"messages": None, "newest": None}
    dates = sorted(
        d for d in ((m.get("date") or "")[:10] for m in messages.values()
                    if isinstance(m, dict)) if d
    )
    return {"messages": len(messages), "newest": dates[-1] if dates else None}


# ============================================================
# (c) Reflect-ready clusters (connected components over raw episodes)
# ============================================================

def _episode_age_days(block: str, today: date):
    """Days since the episode was logged (frontmatter `created`, falling back to
    the event `date`). None when neither field parses -- such a node carries no
    age and is ignored for staleness."""
    for key in ("created", "date"):
        raw = _fm_scalar(block, key)
        if raw:
            try:
                d = date.fromisoformat(raw[:10])
            except ValueError:
                continue
            return (today - d).days
    return None


def _is_unreviewed(block: str, last_reflect: str | None) -> bool:
    """True when this episode was logged after the last CEO-confirmed reflect pass.

    No marker means nothing has ever been reviewed, so every episode counts. An
    episode whose date fields do not parse also counts: an undatable episode that
    went silent would be material lost from the signal, and the safe direction for
    a nudge is to surface it.
    """
    if not last_reflect:
        return True
    # PARSED, not compared as text (a string compare read some malformed dates
    # as already reviewed, MEASURED 2026-08-31), and BOTH keys are tried, so a
    # malformed `created` falls through to a good `date`.
    for key in ("created", "date"):
        raw = _fm_scalar(block, key)
        if not raw:
            continue
        try:
            episode_day = date.fromisoformat(raw[:10])
        except ValueError:
            continue
        try:
            marker_day = date.fromisoformat(last_reflect[:10])
        except ValueError:
            # An unreadable marker cannot establish that anything was reviewed.
            return True
        return episode_day > marker_day
    return True


def analyze_reflect_clusters(root: Path, today: date | None = None) -> dict[str, Any]:
    """Connected components (size >= 2) of raw-status episodes sharing at least
    CLUSTER_MIN_SHARED tags, counted only where they hold material the CEO has not
    reviewed. Transitive A-B-C membership is intended. Returns:

      count            -- clusters carrying at least one UNREVIEWED episode
      reviewed_count   -- clusters that FORMED but carry nothing unreviewed, so
                          they are not counted above. Reported because `count`
                          alone cannot tell "no cluster formed" from "the CEO
                          looked at every one already", and those two zeros ask
                          for opposite actions: the first says the brain has no
                          clusterable material, the second says the reflect side
                          is current.
      stale_count      -- of those, the ones whose oldest unreviewed episode has
                          waited >= STALE_CLUSTER_DAYS
      oldest_age_days  -- longest such wait (None when none datable)
      ages             -- per-counted-cluster wait in days (None where undatable)
      clusters         -- per-counted-cluster {episodes, shared_tags, age_days}
                          detail, same order as `ages`

    **Unreviewed** means logged strictly after `.last-reflect`, the marker
    `/odin reflect` writes on a CEO-confirmed maturation pass. This is what makes
    the signal clearable: a cluster the CEO looked at and deliberately did not graduate now goes quiet
    until it is fed again.

    An episode logged the same day as the pass waits until tomorrow, because the
    marker carries a date and not a time. That is a one-day latency on a
    weekly-cadence signal, and the alternative (>=) restores the un-clearable
    behaviour for the whole day of the pass.

    A cluster's age is the wait of its OLDEST unreviewed episode: how long the
    thing you have not looked at has been sitting."""
    if today is None:
        today = datetime.now(get_default_tz()).date()
    base = _odin_brain(root) / EPISODES_DIR
    empty = {"count": 0, "reviewed_count": 0, "stale_count": 0,
             "oldest_age_days": None, "ages": [], "clusters": []}
    if not base.is_dir():
        return empty

    last_reflect = read_reflect_marker(root)

    nodes = []  # list of (set_of_tags, age_days|None, filename, unreviewed)
    for p in sorted(base.glob("*.md")):
        # The third walker in this file, fixed in the same change as the other
        # two rather than one at a time. A note this cannot open is a note that
        # cannot join a cluster, and a cluster silently missing a member is a
        # nudge the operator never gets.
        try:
            raw_note = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"[warn] odin-cadence: skipping unreadable note {p}: {exc}",
                  file=sys.stderr)
            continue
        block = _frontmatter_block(raw_note)
        if _fm_scalar(block, "status") != "raw":
            continue
        tags = set(_fm_list(block, "entities")) | set(_fm_list(block, "keywords"))
        nodes.append((tags, _episode_age_days(block, today), p.name,
                      _is_unreviewed(block, last_reflect)))

    n = len(nodes)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        for j in range(i + 1, n):
            if len(nodes[i][0] & nodes[j][0]) >= CLUSTER_MIN_SHARED:
                union(i, j)

    members: dict[int, list[int]] = {}
    for i in range(n):
        members.setdefault(find(i), []).append(i)

    ages: list[int | None] = []
    clusters: list[dict[str, Any]] = []
    reviewed_count = 0
    for idxs in members.values():
        if len(idxs) < 2:
            continue
        unreviewed = [i for i in idxs if nodes[i][3]]
        if not unreviewed:
            # The CEO has already seen every member; not a nudge. COUNTED on the
            # way past, because this `continue` and the one above it both land on
            # the same 0 downstream and mean opposite things.
            reviewed_count += 1
            continue
        waits = [nodes[i][1] for i in unreviewed if nodes[i][1] is not None]
        age = max(waits) if waits else None  # the oldest thing not yet looked at
        ages.append(age)
        # `shared_tags` is the tag UNION, a consumed shape: do not narrow it.
        # With transitive A-B-C membership few tags are common to every member;
        # `common_tags` carries that intersection and the report labels both.
        tag_sets = [set(nodes[i][0]) for i in idxs]
        shared_tags = sorted(set().union(*tag_sets)) if tag_sets else []
        common_tags = sorted(set.intersection(*tag_sets)) if tag_sets else []
        clusters.append({
            "episodes": [nodes[i][2] for i in idxs],
            "shared_tags": shared_tags,
            "common_tags": common_tags,
            "age_days": age,
        })

    datable = [a for a in ages if a is not None]
    return {
        "count": len(ages),
        "reviewed_count": reviewed_count,
        "stale_count": sum(1 for a in datable if a >= STALE_CLUSTER_DAYS),
        "oldest_age_days": max(datable) if datable else None,
        "ages": ages,
        "clusters": clusters,
    }


# ============================================================
# Compute + render
# ============================================================

def compute(root: Path, min_entries: int, today: date | None = None) -> dict[str, Any]:
    """The whole cadence verdict for one brain root.

    `today` is the date every age here is measured against, defaulting to the
    CONFIGURED zone's today. It exists because `analyze_reflect_clusters` already
    took one and this function did not pass it, so `stale_clusters` and the
    `stale_clusters>=1` reason were computed against the host clock even when the
    caller had a date in hand. Every test of those two therefore passed only
    while the machine's clock sat far enough past its fixture dates: MEASURED
    2026-09-01, the same fixture (two clusters of episodes created 2026-01-01)
    reported `stale_clusters=2` with the real clock and `stale_clusters=0` with
    the module's clock frozen at 2026-01-05. Threading the date through makes the
    ageing decidable by the caller, as it already was one function down.

    Nothing else moves: omitting the argument is exactly the previous behaviour.
    """
    marker, days_since = read_marker(root)
    mstate = marker_state(root)
    since = marker[:10] if marker else EPOCH_FLOOR
    skipped: list[str] = []
    if mstate == "unreadable":
        skipped.append("collect marker unreadable; counting from the epoch floor")

    thread_n = count_threads(root, since, skipped)
    crm_n = count_crm(root, since, skipped)
    inbox_n = count_assistant_inbox(root, since, skipped)
    total = thread_n + crm_n + inbox_n
    ca = analyze_reflect_clusters(root, today=today)
    clusters = ca["count"]

    reasons = []
    if days_since is None or days_since >= DAYS_THRESHOLD:
        if days_since is None:
            reasons.append("collect marker unreadable" if mstate == "unreadable"
                           else "never collected")
        else:
            reasons.append(f"days_since>={DAYS_THRESHOLD}")
    if total >= min_entries:
        reasons.append(f"unharvested>={min_entries}")
    if clusters >= 1:
        reasons.append("reflect_clusters>=1")
    if ca["stale_count"] >= 1:
        # The THRESHOLD, like every sibling reason on this list. This one
        # interpolated the observed COUNT, so three stale clusters read
        # `stale_clusters>=3` -- indistinguishable in shape from a configuration
        # whose stale threshold happens to be 3, and unusable by a consumer
        # diffing reasons against thresholds. The count is already in the JSON,
        # under `stale_clusters`.
        reasons.append("stale_clusters>=1")
    nudge = bool(reasons)

    return {
        "last_collect": marker,
        "marker_state": mstate,
        "last_reflect": read_reflect_marker(root),
        "days_since": days_since,
        "unharvested_total": total,
        "by_source": {"thread": thread_n, "crm": crm_n, "assistant_inbox": inbox_n},
        "assistant_inbox_source": assistant_inbox_source_state(root),
        "reflect_clusters": clusters,
        "reviewed_clusters": ca["reviewed_count"],
        "stale_clusters": ca["stale_count"],
        "oldest_cluster_age_days": ca["oldest_age_days"],
        "cluster_detail": ca["clusters"],
        "min_entries": min_entries,
        "nudge": nudge,
        "reasons": reasons,
        "skipped": skipped,
    }


def write_cadence_report(root: Path, result: dict[str, Any], today: date) -> Path | None:
    """Write a dated report of cluster membership when `cluster_detail` is
    non-empty; return the path, or None (writes nothing) on a clean week.

    Deliberately different from dream-shadow's always-write posture: this
    report PREVIEWS content (which episodes cluster, on what tags) rather
    than gating a mechanical defect, so an empty week writes nothing.

    The destination is the registry name `odin-cadence-reports`, resolved
    against `root`, never a path joined onto it."""
    clusters = result.get("cluster_detail") or []
    if not clusters:
        return None
    report_dir = data_path("odin-cadence-reports", data_root=root)
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{today.isoformat()}_odin-cadence_report.md"
    lines = [
        "# Odin Cadence Report", "",
        f"**Generated:** {today.isoformat()}", "",
        f"## Reflect-Ready Clusters: {len(clusters)}", "",
    ]
    for c in clusters:
        tags = ", ".join(c["shared_tags"]) if c["shared_tags"] else "(none)"
        common = ", ".join(c.get("common_tags") or []) or "(none in common)"
        age = f"{c['age_days']}d" if c["age_days"] is not None else "unknown age"
        # "oldest unreviewed", not "newest". `age_days` is max(waits) -- the wait
        # of the OLDEST thing not yet looked at -- and the compute docstring
        # spends a paragraph on why, condemning the newest reading as "measured
        # the opposite thing". The report label reintroduced it for the CEO.
        lines.append(f"- Episodes: {', '.join(c['episodes'])} "
                     f"| tags across the cluster: {tags} "
                     f"| in every member: {common} "
                     f"| oldest unreviewed {age}")
    lines.append("")
    text = "\n".join(lines)
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return path


def _no_marker_phrase(r: dict[str, Any]) -> str:
    """What to say when there is no usable date: absent and corrupt differ.

    Both reach here as `days_since is None`, and both used to read "collect
    never run" -- so an unreadable marker told the CEO, over Telegram, that a
    tool he runs weekly had never been run at all.
    """
    return ("collect marker unreadable" if r.get("marker_state") == "unreadable"
            else "collect never run")


def suggestion_line(r: dict[str, Any], report_rel: str | None = None) -> str:
    if not r["nudge"]:
        days = r["days_since"]
        when = f"last collect {days}d ago" if days is not None else _no_marker_phrase(r)
        return f"Odin cadence: up to date ({when}, no new harvestable entries)."

    days = r["days_since"]
    when = f"collect last ran {days}d ago" if days is not None else _no_marker_phrase(r)
    bs = r["by_source"]
    total = r["unharvested_total"]
    parts = []
    if total:
        entry_word = "entry" if total == 1 else "entries"
        parts.append(
            f"{total} un-harvested {entry_word} "
            f"({bs['thread']} threads / {bs['crm']} CRM / {bs['assistant_inbox']} assistant inbox)"
        )
    clusters = r["reflect_clusters"]
    if clusters:
        cluster_word = "cluster" if clusters == 1 else "clusters"
        seg = f"{clusters} {cluster_word} ready to reflect"
        if r.get("stale_clusters"):
            seg += f" ({r['stale_clusters']} stale, oldest {r['oldest_cluster_age_days']}d)"
        parts.append(seg)

    # A zero assistant-inbox count beside a store whose newest message predates the marker is not
    # "nothing new since the collect", it is a feed that stopped. The count alone
    # cannot say which, so the store's own newest date is put beside it and the
    # reader decides. Said only when the count IS zero and the store was readable
    # and holds something: a zero explained by an empty store needs no sentence,
    # and an unreadable one is already in `skipped`.
    src = r.get("assistant_inbox_source") or {}
    marker = (r.get("last_collect") or "")[:10]
    if (not bs.get("assistant_inbox") and src.get("messages") and src.get("newest")
            and marker and src["newest"] < marker):
        parts.append(
            f"assistant inbox quiet since {src['newest']} "
            f"({src['messages']} messages held, none after the marker)"
        )

    tail = []
    if total:
        tail.append("/odin collect")
    if clusters:
        tail.append("/odin reflect")
    detail = ", ".join(parts) if parts else "cadence due"
    # A days-only nudge (no new entries, no clusters) left `tail` empty and this
    # line, sent verbatim to Telegram, ended "Run .".
    line = f"Odin cadence: {when} — {detail}."
    if tail:
        line += f" Run {' or '.join(tail)}."
    if report_rel:
        line += f" (report: {report_rel})"
    return line


# ============================================================
# CLI
# ============================================================

def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only Odin collect/reflect cadence nudge.")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    ap.add_argument("--quiet", action="store_true", help="print nothing unless a nudge is due")
    ap.add_argument("--min-entries", type=int, default=DEFAULT_MIN_ENTRIES,
                    help=f"un-harvested threshold (default {DEFAULT_MIN_ENTRIES})")
    args = ap.parse_args()

    # All sources counted here are DATA (threads, crm, knowledge, assistant-inbox state),
    # so resolve under the DATA root via the data-root seam -- never the engine
    # clone. `root` below is the data root throughout this module.
    root = get_data_root()
    today = datetime.now(get_default_tz()).date()
    r = compute(root, args.min_entries)
    report_path = write_cadence_report(root, r, today)
    report_rel = str(report_path.relative_to(root)) if report_path else None

    if args.json:
        out = dict(r)
        out["report_path"] = report_rel
        print(json.dumps(out, indent=2, default=str))
        return 0
    if args.quiet and not r["nudge"]:
        return 0
    print(suggestion_line(r, report_rel))
    return 0


if __name__ == "__main__":
    sys.exit(main())
