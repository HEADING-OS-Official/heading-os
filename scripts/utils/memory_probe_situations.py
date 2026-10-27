"""The situational probe: does the rule surface when nobody asked for it?

**This is the measurement that decides whether a resident index can shrink.** It
does not ask "where do I write temporary files?"; it states a SITUATION ("I am
starting a long run and need somewhere to put the log") and scores whether the
right record comes back. Nobody asked a question.

Why the distinction is the whole point. `MEMORY.md` is loaded into context on
every session, so it fires with no decision from anyone. Retrieval fires only
when someone suspects there is something to find, and the failure mode of a
behavioural rule is exactly that nobody suspects: an agent about to write to
`/tmp` does not think "let me search for rules about temporary files", it just
writes to `/tmp`. A retrieval system that answers questions perfectly can still
be useless as a replacement for resident text, and measuring it on questions
would never show that.

THE ONE RULE FOR WRITING A SITUATION, and it is what makes this honest:

    The situation must not contain the distinctive vocabulary of the record it
    targets.

If the situation for the `/tmp` rule says "temporary files", it has handed the
retriever the answer and this becomes the question probe with extra words.
Every case therefore carries `avoided`: the terms deliberately kept OUT, so a
later reader can check the discipline rather than trust it. Nothing checks
`avoided` mechanically now, so keep the rule by hand on every edit.

TWO PACKS, SCORED SEPARATELY AND NEVER AVERAGED. `conduct` holds rules,
prohibitions and traps; `lookup` holds facts one would look up. The split exists
because the first run of `conduct` answered its question and, in answering it,
exposed what it could not answer: 25 of its 30 targets carry `type: feedback` and
exactly one carries `type: reference`, so it measured the half that must STAY
resident and said nothing about the half that would actually move into retrieval.
One figure over both would hide precisely the difference the split decision turns
on.

WHY THE CASES ARE NOT IN THIS FILE, and this is not a tidiness preference. Each
case names a RECORD, and in this corpus a filename IS the fact: one target names
a counterparty and a deal, another a customer and a bid, a third a colleague.
MEASURED 2026-09-10, the content guard found eleven such entities in the table
that used to live here and refused the commit. It refused this docstring too, on
the pair of filenames that stood where this sentence does, which is the lesson
arriving twice in one edit. It was right both times: this repository is public,
and a probe
that carried the operator's counterparties into it would be the very defect one
of its own cases exists to catch
(the rule that a measurement quoted into the engine carries real names with
it). So the engine ships the
mechanism and the overlay holds the corpus, which is the same seam every other
data-touching script here uses.

A clone with no overlay therefore has no cases. That is a normal state, not a
failure: `situations()` raises with the path it looked in.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from scripts.utils.data_path import data_path


# One file per pack, under that address in the DATA overlay.
CASES_DIRNAME = "situations"
PACK_NAMES = ("conduct", "lookup")
SID_PREFIX = {"conduct": "situation", "lookup": "lookup"}


@dataclass
class Situation:
    """One moment of work, and the record that should surface unbidden."""

    sid: str
    text: str
    target: str
    avoided: tuple[str, ...] = ()
    lang: str = "ru"
    note: str = ""
    acceptable: tuple[str, ...] = field(default_factory=tuple)

    def hit(self, returned: list[str]) -> bool:
        wanted = {self.target, *self.acceptable}
        return any(any(p.endswith(f"/{w}") or p == w for w in wanted if w)
                   for p in returned)


class CasesUnavailableError(FileNotFoundError):
    """The corpus lives in the overlay and this clone has none."""


def cases_dir() -> Path:
    # The address is the name the folder was registered under by the memory
    # pilot that first wrote it. HOS-198 removed that pilot on 2026-10-04 and
    # left the address alone: renaming it is a move in the data repository, not
    # an edit here. Spelled at the call so the leak guard can read it.
    return data_path("cognee-pilot-outputs") / CASES_DIRNAME


def case_file(pack: str) -> Path:
    """The pack's JSON under the registered address (a plain join since HOS-5
    phase 6, 2026-09-22)."""
    return cases_dir() / f"{pack}.json"


@lru_cache(maxsize=None)
def _load(pack: str) -> tuple[tuple[str, str, tuple[str, ...], str], ...]:
    path = case_file(pack)
    if not path.exists():
        raise CasesUnavailableError(
            f"no case file for pack {pack!r} at {path}. The probe corpus lives "
            "in the data overlay because its case names are private records; a "
            "clone without the overlay cannot run this measurement.")
    rows = json.loads(path.read_text(encoding="utf-8"))
    return tuple((r["target"], r["text"], tuple(r["avoided"]), r.get("note", ""))
                 for r in rows)


def unreadable_packs() -> tuple[tuple[str, Path], ...]:
    """Registered packs whose case file could not be READ, each with its path.

    The distinction `packs()` cannot draw on its own, and the whole of the
    2026-09-16 defect. "This pack holds no cases" and "I could not find this
    pack's file" are different answers, and until this function existed they
    were the same one: `packs()` dropped both, so a corpus the HEADING OS 2 move
    had relocated read as a corpus that was simply empty.

    Asked of the same `_load` the menu is built from, so the two cannot drift.
    `lru_cache` does not cache exceptions, so a pack that is missing is re-asked
    on every call; that cost is paid only on the broken path, which then
    refuses.
    """
    out = []
    for name in PACK_NAMES:
        try:
            _load(name)
        except CasesUnavailableError:
            out.append((name, case_file(name)))
    return tuple(out)


def packs() -> tuple[str, ...]:
    """The packs this clone can actually measure.

    An EMPTY pack is never offered. A pack that is registered but holds nothing
    is vacuous in both directions: a discipline run would iterate zero cases and
    pass, and it would print a recall of `None` over `asked: 0`. So a name
    appears here only when its file exists and carries cases.

    TOTAL, and it never raises: it is a MENU query, built before any
    measurement is asked for, and a refusing menu killed a whole CLI on a clone
    without the overlay (MEASURED 2026-09-16). The refusal belongs at the
    measurement boundary, in `situations()`, which uses `unreadable_packs()`.
    """
    out = []
    for name in PACK_NAMES:
        try:
            if _load(name):
                out.append(name)
        except CasesUnavailableError:
            continue
    return tuple(out)


def situations(pack: str = "conduct") -> list[Situation]:
    """One pack's cases, or every case when `pack` is "all".

    The default is `conduct` so the 2026-09-10 measurement stays reproducible by
    the command that produced it. A new pack must be asked for by name.

    "all" REFUSES when any registered pack could not be read, because "all"
    means all and a pack whose file is gone has not been measured. MEASURED
    2026-09-16 on the migration rehearsal tree, before this refusal existed:
    both packs had moved into the `("trace", "scratch")` kind, this function answered `[]`,
    and `memory-index-probe.py score` divided by zero over 0 moments rather
    than naming either missing file. Four layers of silence (a legacy directory
    that survived empty, `_load` raising per pack, `packs()` dropping both,
    `"all"` reading the empty menu as the answer) became one message.

    A pack that is genuinely EMPTY still does not refuse: its file was read, it
    holds nothing, and `packs()` declines to offer it. That is the case the
    `packs()` docstring is about and it is untouched.
    """
    available = packs()
    if pack == "all":
        unreadable = unreadable_packs()
        if unreadable:
            where = "; ".join(f"{name} at {path}" for name, path in unreadable)
            raise CasesUnavailableError(
                f"cannot measure {pack!r}: {len(unreadable)} of "
                f"{len(PACK_NAMES)} registered pack(s) could not be read "
                f"({where}). Measuring the packs that survived would report a "
                "number over part of the corpus as if it were the whole.")
        chosen = available
    elif pack in PACK_NAMES:
        chosen = (pack,)
    else:
        raise KeyError(f"unknown pack {pack!r}; have {list(PACK_NAMES)} and 'all'")

    out: list[Situation] = []
    for name in chosen:
        prefix = SID_PREFIX[name]
        out.extend(
            Situation(sid=f"{prefix}-{i:02d}", text=text, target=target,
                      avoided=avoided, note=note)
            for i, (target, text, avoided, note) in enumerate(_load(name), start=1)
        )
    return out
