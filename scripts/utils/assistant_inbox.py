#!/usr/bin/env python3
"""assistant inbox counterpart resolver -- the content-class gate for /odin collect.

assistant inbox is the only collect source with no path boundary (a single free-text
Telegram channel), so business-counterpart resolution is its ONLY air-gap. A
message is admitted into episode collection ONLY when it resolves to a real
EXTERNAL business counterpart: a named person or company in the `crm-contacts`
or `people` records whose relationship is not purely internal tribe. Both are
resolved by registry name, through `crm.contacts_home` and `people_file`
below; by hand, `python scripts/data-path.py path crm-contacts`.

Two failure modes from the first live run (2026-05-30) shaped this, neither of
which a name-match-alone gate can stop:

  1. Noise-token leak. Generic words (`channel`, `document`, `from`, `with`)
     harvested from CRM *bodies* resolved a personal apostille task. Fix: the
     vocabulary is built ONLY from structured name fields (CRM frontmatter
     name / entity_ref / pipeline_company, aliases.md company names, people.md
     section headers and top-contact lead names) -- never from free-text
     bodies -- plus a stoplist of generic and company-suffix words.

  2. Internal-personal content. "check with Alex re a colleague's case" names two
     real people, but both are tribe -- a personal matter, not a business
     episode. Fix: require at least one EXTERNAL (non-tribe) counterpart. A
     message resolving only to tribe members drops (and is counted).

Pure string transform over already-captured residue. No network, no filesystem
writes. Imported by the /odin collect mode; also runnable as a CLI to gate a
assistant-inbox state.json end-to-end.

Usage:
    from scripts.utils.assistant_inbox import build_vocab, resolve, gate_message
    vocab = build_vocab(root)
    r = resolve(text, vocab)              # -> {"external": [...], "tribe": [...]}
    admit = bool(r["external"])           # gate: >=1 external counterpart

    python3 scripts/utils/assistant_inbox.py --since 2026-05-19   # gate report
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from scripts.utils.markdown import FM_OK, split_frontmatter
from scripts.utils.data_path import data_path
# The `crm-contacts` directory under a root comes from the one resolver in
# `crm`; re-exported here because `scripts/odin-cadence.py` calls it by this name.
from scripts.utils.crm import contacts_home
from scripts.utils.workspace import get_data_root

# ============================================================
# Configuration
# ============================================================


class VocabularyEmpty(RuntimeError):
    """The counterpart vocabulary came back with nothing usable in it.

    Raised rather than returned, and it is the same call this module already
    makes one screen down for a card that vanishes mid-walk: a vocabulary
    missing its CRM cards does not make the gate see LESS, it makes the gate
    decide DIFFERENTLY, and neither direction is an answer.

    With no tokens at all, `resolve` matches nothing, `gate_message` drops every
    message as "no business counterpart", and `/odin collect` reports a clean
    zero over a live channel. With SOME tokens and no CRM cards, the tribe class
    is never populated, so `_add`'s "a token seen as tribe is never promoted to
    external" holds over nothing and people.md registers a tribe surname as
    external -- failure mode 2 in the module docstring, internal-personal
    content admitted into episode collection.
    """


# The four hand-copied (family, leaf) tuples that stood here were removed
# 2026-09-22: the registry entry carries the pair, one copy instead of two.

# Generic words that must never resolve a message to a counterpart. Two groups:
# company-suffix / role words that ride along inside structured name fields, and
# common English/Russian filler that surfaced as noise in the first live run.
STOPLIST = {
    # company suffixes and generic business words
    "group", "capital", "beyond", "holding", "holdings", "partners", "partner",
    "company", "communications", "technologies", "technology", "solutions",
    "systems", "consulting", "ventures", "labs", "inc", "ltd", "llc", "fz",
    "the", "and", "for", "dc", "one", "concept", "international", "global",
    "sales", "services", "service", "digital", "networks", "network",
    # tribe / role nouns
    "ceo", "cto", "cso", "coo", "svp", "vp", "founder", "principal", "tribe",
    # observed noise tokens (belt-and-braces; structured-only build already excludes them)
    "channel", "document", "from", "with", "per", "get", "ask", "check",
    "added", "task", "deleted", "regarding", "case", "advice", "paper",
    "status", "action", "needed", "request", "support", "them", "their",
    "advice's", "employer", "via", "today", "tomorrow", "meeting", "email",
}

_WORD = re.compile(r"[A-Za-zЀ-ӿ]+")


#: The operator's own company as CRM records write it (`company_short` in
#: operator.yaml, e.g. "ACME"); a counterpart at it is internal. Empty on a clone
#: that sets none, and then nobody is classed internal by company.
def _own_company() -> str:
    from scripts.utils.operator_identity import company_brand
    return company_brand()[1].strip()


_OWN = _own_company()


def _name_tokens(s: str):
    """Lowercase alphabetic tokens of length >= 3 from a person/company name."""
    if not s:
        return []
    return [t for t in (w.lower() for w in _WORD.findall(s)) if len(t) >= 3]


def _slug_tokens(slug: str):
    """Parts of a kebab-case slug, length >= 3."""
    return [p.lower() for p in (slug or "").split("-") if len(p) >= 3 and p.isalpha()]


def _frontmatter(text: str) -> dict:
    """Flat scalar frontmatter, read with a line regex. `{}` when there is none.

    Fences via the shared splitter since 2026-08-28. `startswith("---")` plus
    `find("\\n---", 3)` tested for the CHARACTERS, so a document opening
    `---extra` was read here and REFUSED by every migrated reader in the engine
    (MEASURED 2026-08-28 over eight documents). Fail-open on a malformed file is
    the wrong direction for a counterpart resolver: it invents a record from a
    file nothing else considers to have frontmatter.

    The line regex stays: this reader wants flat scalars only and must not turn a
    numeric-looking value into an int the way `yaml.safe_load` would.
    """
    block, _body, kind = split_frontmatter(text)
    if block is None or kind != FM_OK:
        return {}
    fm = {}
    for line in block.splitlines():
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if m:
            fm[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return fm


# ============================================================
# Addresses, by registry name under the root each caller hands in
# ============================================================
#
# Converted from the migration seam 2026-09-22: `data_path(..., data_root=root)`
# answers for any root, an empty fixture directory included, through the
# registry example's fallback.

def aliases_file(root: Path) -> Path:
    """The `crm-aliases` file under `root`, before and after the move."""
    return data_path("crm-aliases", data_root=Path(root))


def people_file(root: Path) -> Path:
    """The `people` file under `root`, before and after the move."""
    return data_path("people", data_root=Path(root))


def state_path(root: Path) -> Path:
    """The assistant inbox `state.json` under `root`, before and after the move.

    Exported because `scripts/odin-cadence.py` reads the same file and must not
    hold a second copy of the address.
    """
    return data_path("assistant-inbox-state", data_root=Path(root))


# ============================================================
# Vocabulary build (structured name fields ONLY)
# ============================================================

def _add(vocab: dict, token: str, cls: str):
    """Insert a token with its class. Conservative on collision: a token seen as
    tribe is never promoted to external, so a surname shared with a tribe member
    cannot silently auto-admit a personal message."""
    if token in STOPLIST or len(token) < 3:
        return
    if cls == "tribe":
        vocab[token] = "tribe"
    else:  # external
        vocab.setdefault(token, "external")


def build_vocab(root: Path, notes: list | None = None) -> dict:
    """token -> 'external' | 'tribe', drawn only from structured name fields.

    Raises `VocabularyEmpty` when the CRM contacts home is not a directory, and
    again when the finished vocabulary holds no token at all. Both were a silent
    zero until 2026-09-18, and the silence was this gate's whole failure.

    MEASURED that day on the operator's overlay: the three addresses this
    function read had all been vacated by the HEADING OS 2 move. The glob over
    the legacy CRM contacts directory returned 0 files against 195 live cards
    where the `crm-contacts` address answers now, and `aliases.md` and `people.md` were
    each skipped by an `exists()` guard that says nothing. `build_vocab` returned
    0 tokens, so every assistant inbox message was dropped as "no business counterpart"
    and `/odin collect` read a clean zero over a channel holding live traffic.
    The module even raises on ONE card vanishing mid-walk; a wholly absent
    directory passed without a word.

    `notes` is the caller's out-parameter for what was resolved but not found --
    the same shape `scripts/odin-cadence.py` already uses for its `skipped` list.
    Absent `aliases.md` and `people.md` stay non-fatal (each can only make the
    vocabulary SMALLER, which fails toward dropping a message rather than
    admitting one), but they are named there instead of vanishing.
    """
    root = Path(root)
    vocab: dict = {}

    # 1. CRM contacts (primary, richest source)
    #
    # REFUSED rather than globbed over nothing. This is the only source that
    # populates the `tribe` class with authority, so an absent directory is the
    # exact shape of failure mode 2 in the module docstring: people.md then
    # registers a tribe surname as `external` and a personal message is admitted
    # into episode collection.
    contacts = contacts_home(root)
    if not contacts.is_dir():
        raise VocabularyEmpty(
            f"no CRM contacts directory at {contacts}. This gate classifies "
            f"counterparts from those cards and is the ONLY air-gap on the "
            f"assistant inbox source, so with none of them read every message either "
            f"drops as 'no business counterpart' or is admitted on a token "
            f"people.md alone called external. That address is the registry "
            f"name 'crm-contacts' resolved against {root}; on a tree with a "
            f"HEADING OS 2 manifest it is the kind's home, and on one without "
            f"it is the pre-move directory. Ask "
            f"`python scripts/data-path.py path crm-contacts` for the same "
            f"answer from a shell."
        )
    for p in sorted(contacts.glob("*.md")):
        # Retried once, then REFUSED, and not the skip-and-warn of
        # `scripts.utils.repo_files.read_sources`. A vocabulary looks like a
        # search corpus, where skipping is right; this one is not. It is the
        # ONLY air-gap on the assistant inbox source (module docstring, first paragraph),
        # and it is a CLASSIFIER input, not a retrieval index. A card dropped
        # here does not make the gate see less, it makes the gate decide
        # differently: a token that this card would have registered `tribe` is
        # either absent (the message drops, and a real business episode is lost
        # with no trace) or registered `external` by people.md instead, which is
        # failure mode 2 in the docstring -- internal-personal content admitted
        # into episode collection. `_add`'s collision rule, "a token seen as
        # tribe is never promoted to external", only holds if the tribe card was
        # actually read.
        #
        # The retry recovers a writer's unlink-and-rewrite window; a card that
        # is genuinely gone is still gone on the second look. Only the vanished
        # case is caught -- `errors="replace"` already covers an undecodable
        # byte, and a permission error is a real fault about a file that IS
        # there.
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            try:
                raw = p.read_text(encoding="utf-8", errors="replace")
            except FileNotFoundError as exc:
                raise RuntimeError(
                    f"{p} vanished between the walk and the read. The CRM tree "
                    f"changed while the counterpart vocabulary was being built "
                    f"over it, and a vocabulary missing one card can admit "
                    f"internal-personal content this gate exists to keep out. "
                    f"Re-run once the tree is quiet."
                ) from exc
        fm = _frontmatter(raw)
        rel = (fm.get("relationship_type") or "").lower()
        company = fm.get("pipeline_company") or ""
        is_tribe = rel.startswith("tribe") or bool(_OWN) and company.strip() == _OWN
        cls = "tribe" if is_tribe else "external"
        for t in _name_tokens(fm.get("name", "")):
            _add(vocab, t, cls)
        for t in _slug_tokens(fm.get("entity_ref") or p.stem):
            _add(vocab, t, cls)
        # company tokens: only meaningful for external partners/deals
        if not is_tribe:
            for t in _name_tokens(company):
                _add(vocab, t, "external")

    # 2. aliases.md -- canonical + variant company names (all external)
    aliases = aliases_file(root)
    if notes is not None and not aliases.exists():
        notes.append(f"assistant-inbox vocab: no aliases file at {aliases}")
    if aliases.exists():
        for line in aliases.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"^###\s+(.+)$", line) or re.match(r"^-\s+(.+)$", line)
            if m:
                for t in _name_tokens(m.group(1)):
                    _add(vocab, t, "external")

    # 3. people.md -- structured lines only (never free-text bodies)
    people = people_file(root)
    if notes is not None and not people.exists():
        notes.append(f"assistant-inbox vocab: no people file at {people}")
    if people.exists():
        for line in people.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            # section headers -> company names (external)
            h = re.match(r"^###\s+(.+)$", stripped)
            if h:
                for t in _name_tokens(h.group(1)):
                    _add(vocab, t, "external")
                continue
            # top-contact bullets: "- Name (role, Company) - ..." ; class by a mention of the own company
            b = re.match(r"^-\s+([A-Z][^()\-]+?)\s*[(\-]", stripped)
            if b:
                cls = "tribe" if _OWN and _OWN in stripped else "external"
                for t in _name_tokens(b.group(1)):
                    _add(vocab, t, cls)
                continue
            # "**Principals:** A and B" -> external person names
            pr = re.match(r"^-?\s*\*\*Principals?:\*\*\s*(.+)$", stripped)
            if pr:
                for t in _name_tokens(pr.group(1)):
                    _add(vocab, t, "external")

    # The second half of the refusal, and it is not the same check as the one
    # above: a contacts directory that EXISTS and yields nothing usable reaches
    # here with the identical consequence. A CRM tree of cards whose frontmatter
    # this reader cannot parse, or a directory emptied by a half-finished move,
    # both arrive as a gate that says no to everything.
    if not vocab:
        cards = len(list(contacts.glob("*.md")))
        raise VocabularyEmpty(
            f"the counterpart vocabulary is empty: {cards} card(s) under "
            f"{contacts}, aliases {'present' if aliases.exists() else 'absent'} "
            f"({aliases}), people {'present' if people.exists() else 'absent'} "
            f"({people}). Every assistant inbox message would drop as 'no business "
            f"counterpart', which is a broken gate reporting a clean pass."
        )
    return vocab


# ============================================================
# Resolution + gate
# ============================================================

def resolve(text: str, vocab: dict) -> dict:
    """Return {'external': [...], 'tribe': [...]} of counterpart tokens named in
    text. Whole-word, case-insensitive; possessives ('Morgan'') collapse to the
    bare token. Only structured-name vocab can match -- generic words cannot."""
    ext, trb = set(), set()
    for w in _WORD.findall((text or "").lower()):
        cls = vocab.get(w)
        if cls == "external":
            ext.add(w)
        elif cls == "tribe":
            trb.add(w)
    return {"external": sorted(ext), "tribe": sorted(trb)}


def gate_message(msg: dict, vocab: dict, since: str):
    """(admit, reason, resolved) for one assistant inbox message dict.

    Admit iff disposition in {task, crm} AND date >= since AND >=1 external
    counterpart resolves. Otherwise drop with a reason string.
    """
    disp = (msg.get("disposition") or "").lower()
    if disp not in ("task", "crm"):
        return False, f"disposition={disp or 'none'}", {"external": [], "tribe": []}
    date = (msg.get("date") or "")[:10]
    # An ABSENT date used to skip the window check and admit. The docstring one
    # screen up says "Admit iff ... date >= since", and a message with no date
    # satisfies no such thing: `date and date < since` is False for "", so every
    # undated message passed. Measured 2026-08-26 with since='2026-01-01': no
    # `date` key admitted, `date: ""` admitted, only `date: '2020-01-01'`
    # dropped. Consequence: `/odin collect` re-harvests the same undated assistant inbox
    # messages as fresh episodes on every run, for as long as they sit there.
    # A missing date is now its own drop reason rather than a silent pass, so
    # the CLI's per-message reason column says which of the two happened.
    if not date:
        return False, "date missing", {"external": [], "tribe": []}
    if date < since:
        return False, f"date<{since}", {"external": [], "tribe": []}
    text = (msg.get("text") or "") + " " + (msg.get("action_summary") or "")
    r = resolve(text, vocab)
    if not r["external"]:
        why = "no business counterpart" if not r["tribe"] else "tribe-only (internal)"
        return False, why, r
    return True, "external counterpart", r


# ============================================================
# CLI -- gate a assistant-inbox state.json end-to-end
# ============================================================

def _run(since: str, root: Path):
    state = state_path(root)
    if not state.exists():
        print(f"assistant-inbox state not found: {state}")
        return 1
    notes: list[str] = []
    try:
        vocab = build_vocab(root, notes)
    except VocabularyEmpty as exc:
        # Exit 2, distinct from the exit 1 above. "the store is not there" and
        # "the gate cannot classify anything" are different failures and only
        # one of them is about the assistant inbox.
        print(f"assistant-inbox gate REFUSED: {exc}", file=sys.stderr)
        return 2
    for note in notes:
        print(f"  note: {note}", file=sys.stderr)
    msgs = json.loads(state.read_text(encoding="utf-8")).get("messages", {})
    kept, dropped = [], {}
    for mid, m in msgs.items():
        admit, reason, r = gate_message(m, vocab, since)
        if admit:
            kept.append((m.get("date", "")[:10], r["external"], m.get("action_summary", "")[:70]))
        else:
            dropped[reason] = dropped.get(reason, 0) + 1
    print(f"assistant-inbox gate  (--since {since})   vocab tokens: {len(vocab)}")
    print(f"  messages: {len(msgs)}  admitted: {len(kept)}  dropped: {sum(dropped.values())}")
    for reason, n in sorted(dropped.items(), key=lambda x: -x[1]):
        print(f"    drop[{reason}]: {n}")
    print("  admitted:")
    for d, ext, summ in sorted(kept):
        print(f"    [{d}] external={ext} :: {summ}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Gate a assistant-inbox state.json for /odin collect.")
    ap.add_argument("--since", default="1970-01-01", help="ISO date floor (YYYY-MM-DD)")
    ap.add_argument("--root", default=None, help="workspace root (default: auto-detect)")
    args = ap.parse_args()
    root = Path(args.root) if args.root else get_data_root()
    return _run(args.since, root)


if __name__ == "__main__":
    raise SystemExit(main())
