"""What the memory bank should hold for each document version family, and what to delete.

`plan()` is the one place that turns the version registry's replayed `State`,
the hindsight-sync ledger and one `doc_scan.ScanResult` into a `Plan`: which
sent paths carry a version header, which are held back, which are history and
never uploaded, and which document ids to DELETE from the bank. It never writes
anything and never calls the network; `scripts/hindsight-sync.py push` does
both, and only after re-checking the plan under the registry lock.

## The rules, per member of a CONFIRMED family with a decided actual version

- The actual version is uploaded with a header (`header_for`), and sent again
  whenever `header_fp()` changes: the header template version, the header text
  (earlier versions, their dates and paths, the change description) and the
  decided revision all feed it. An in-place edit of the actual file keeps it
  actual; the ordinary SHA-256 check in hindsight-sync resends it.
- A proposed member is held: not uploaded until decided, memory unchanged.
- A history member is not uploaded, and its previously uploaded documents are
  deleted, but only when the file now at that path AND the ledger's last upload
  for it are both OURS. A hash is ours when the registry ever associated it
  with the member (`Member.known_shas`: the proposal, every `relocated`, every
  `decided`), or when the ledger's last upload for that path carried a
  `header_fp` (it was sent as a family's actual version) and recorded exactly
  that hash. So a former actual edited in place, before or after its decision,
  is still removed once a newer version is decided. Content at a history path
  that is not ours is new content at a reused path: it is sent as an ordinary
  file and never deleted.
- While the actual version is `missing` or `unavailable`, every history member
  of that family is held and nothing of it is deleted; an error note says so.
  That includes new content at a reused history path: it is held too, until
  the family's actual version is back or a new one is decided.
- A `missing` member (two consecutive complete scans without it) has its
  documents deleted, the mirror rule of M003, under the same ledger check (for
  history, only while the actual is available). An `unavailable` member gets
  nothing; no replacement actual is ever chosen automatically.
- A member in an archive folder below a scope root is looked at like any other
  (`ScanResult.archived`, 2026-10-12); the detector still never proposes one.
- A member the scan did not look at (`ScanResult.out_of_scope`) or saw only
  mid-edit (`ScanResult.skipped`) is held and never deleted.

A family that is unconfirmed, or confirmed with no decided actual version,
changes nothing: its files are uploaded exactly as before. Deciding nothing is
not a reason to empty the memory of a family.

## What makes a deletion possible at all

`result.complete` must be True (never gated on `errors`). Every id comes from
the ledger, never from a naming rule alone: the ids a path's upload lines show
were sent (the path itself, or `<path>#part-NN` for a file sent in N parts,
counted from the line's `operation_ids`), minus the ids a later `deleted` line
already records. A document sent through its `-extract.md` twin contributes the
twin's ledger ids only when the twin file on disk names a SHA-256 of the member in
its provenance header AND carries the bytes the ledger last recorded for it; a
twin that is absent or belongs to another original contributes nothing. Then
the guard: more than `max_deletes`, or more than `max_fraction` of the paths the
ledger shows as uploaded, empties the delete list and sets `blocked`. Before
the guard, any delete whose ledger path this same plan uploads or holds, or
that the scan saw only mid-edit, is dropped: a path is never sent and deleted
in one pass, whatever the registry says about who owns it.
"""
from __future__ import annotations

import hashlib
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

_HERE = Path(__file__).resolve()
if str(_HERE.parents[2]) not in sys.path:
    sys.path.insert(0, str(_HERE.parents[2]))

from scripts.utils import doc_scope  # noqa: E402
from scripts.utils import version_registry as vr  # noqa: E402
from scripts.utils.doc_scan import ScanResult  # noqa: E402

_LOGGER = logging.getLogger(__name__)

__all__ = ["HEADER_TEMPLATE_VERSION", "Plan", "plan", "header_for", "header_fp",
           "index_ledger", "document_ids_for", "uploaded_count", "SENT_AS_ITSELF",
           "MAX_DELETES_PER_DAY", "family_deletions_since", "cap_daily"]

#: Bumped whenever the wording or layout of `header_for` changes, so every
#: actual version is sent again with the new header.
HEADER_TEMPLATE_VERSION = "1"

#: Extensions hindsight-sync sends as themselves; every other tracked format
#: reaches the bank only through its `-extract.md` twin. hindsight-sync's own
#: `TEXT_EXT` is derived from this one set. Until 2026-10-12 this held only
#: `.md` and `.csv` while hindsight-sync also sent `.txt`, `.yaml` and `.yml`,
#: so a family whose actual version was a `.txt` file had it sent with no
#: header, and its `.txt` history neither held nor deleted.
SENT_AS_ITSELF = frozenset({".md", ".txt", ".csv", ".yaml", ".yml"})

_UNKNOWN_DATE = "дата неизвестна"

#: The deletion guard of one pass: at most this many ids, and at most this
#: fraction of the paths the ledger shows as uploaded. `plan()` blocks a family
#: batch over it; hindsight-sync's mirror step deletes up to it and leaves the
#: rest for the next passes (HOS-188).
MAX_DELETES = 20
MAX_FRACTION = 0.01

#: The cumulative cap on FAMILY deletions over any 24 hours, across passes
#: (2026-10-12, HOS-178). The per-pass guard above allows 20 a pass and the
#: timer runs 96 passes a day, so without this a steady trickle under the guard
#: could empty far more of the bank in a day than anyone looked at. The value
#: is the yard's proposal, two full passes' worth; the operator may set another.
#: Counted from the ledger's `delete_intent` lines that carry a `generation`
#: (the family deletion path writes it; the mirror step writes `mirror`
#: instead and is not counted), so an attempt whose outcome is unknown counts.
MAX_DELETES_PER_DAY = 40


@dataclass
class Plan:
    upload: dict[str, str] = field(default_factory=dict)  # sent path -> header text
    hold: set[str] = field(default_factory=set)
    delete: list[str] = field(default_factory=list)  # document ids
    blocked: str | None = None
    skip: set[str] = field(default_factory=set)  # history: never uploaded
    delete_path: dict[str, str] = field(default_factory=dict)  # document id -> ledger path
    fingerprints: dict[str, str] = field(default_factory=dict)  # sent path -> header_fp
    reasons: dict[str, str] = field(default_factory=dict)  # held path -> why
    notes: list[str] = field(default_factory=list)


def header_fp(header: str, actual_sha: str) -> str:
    """The fingerprint of the whole published representation of an actual version."""
    return hashlib.sha256((HEADER_TEMPLATE_VERSION + header + actual_sha).encode("utf-8")).hexdigest()


def _latest_summary(fam: vr.Family) -> str | None:
    """The most recently stored summary whose `to_sha` is the decided revision."""
    found = None
    for (_from_sha, to_sha), text in fam.summaries.items():  # insertion order is replay order
        if to_sha == fam.actual_sha:
            found = text
    return found


def header_for(fam: vr.Family, dates: dict[str, str]) -> str:
    """The neutral header an actual version carries. `dates` maps artifact_id to a date.

    Lists the earlier versions by date and path and quotes the stored change
    description. It never includes file content or an old value: the change
    description is the only free text, and HELM writes it neutrally.
    """
    actual = fam.members[fam.actual]
    earlier = sorted((dates.get(aid, _UNKNOWN_DATE), m.path)
                     for aid, m in fam.members.items() if aid != fam.actual)
    listed = "; ".join(f"{d} {p}" for d, p in earlier) or "нет"
    summary = _latest_summary(fam) or "не описаны"
    return (f"Актуальная версия от {dates.get(actual.artifact_id, _UNKNOWN_DATE)}. "
            f"Прежние версии: {listed}. Изменения: {summary}.")


def _ids_of_upload(path: str, rec: dict) -> list[str]:
    ops = rec.get("operation_ids") or []
    if len(ops) > 1:
        return [f"{path}#part-{i:02d}" for i in range(1, len(ops) + 1)]
    return [path]


def index_ledger(lines: list[dict]) -> dict[str, dict]:
    """Per path: the last upload line, every id ever sent, and the ids since deleted.

    Upload lines carry `sha256`; a later upload of an id makes it live again.
    `requeue` and `delete_intent` lines change nothing here: an intent with no
    result is a deletion whose outcome is unknown, so the id stays live and the
    next pass may safely ask again.
    """
    idx: dict[str, dict] = {}
    for rec in lines:
        path = rec.get("path")
        if not path:
            continue
        entry = idx.setdefault(path, {"upload": None, "ids": [], "deleted": set()})
        if "deleted" in rec:
            entry["deleted"].add(rec["deleted"])
        elif "sha256" in rec and "requeue" not in rec:
            for doc_id in _ids_of_upload(path, rec):
                if doc_id not in entry["ids"]:
                    entry["ids"].append(doc_id)
                entry["deleted"].discard(doc_id)
            entry["upload"] = rec
    return idx


def family_deletions_since(lines: list[dict], since: datetime) -> int:
    """How many family deletions the ledger shows attempted at or after `since`.

    A `delete_intent` line with a `generation` key is written right before each
    family DELETE; its `at` is an ISO timestamp. A line whose `at` does not
    parse is counted, so a damaged line can only make the cap tighter.
    """
    n = 0
    for rec in lines:
        if "delete_intent" not in rec or "generation" not in rec:
            continue
        try:
            at = datetime.fromisoformat(str(rec.get("at")))
        except ValueError:
            n += 1
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if at >= since:
            n += 1
    return n


def cap_daily(p: "Plan", used: int, cap: int = MAX_DELETES_PER_DAY) -> "Plan":
    """Cut `p.delete` to what the last 24 hours leave of `cap`; the rest waits.

    Applied after `plan()`, so the per-pass guard has already judged the whole
    batch. The ids beyond the allowance stay planned for a later pass, the same
    "in portions" rule the mirror step follows; a note says how many wait.
    """
    left = max(0, cap - used)
    if len(p.delete) <= left:
        return p
    waiting = p.delete[left:]
    p.delete = p.delete[:left]
    for doc_id in waiting:
        p.delete_path.pop(doc_id, None)
    p.notes.append(f"daily cap: {used} family deletion(s) in the last 24 hours, cap {cap}; "
                   f"{len(waiting)} planned deletion(s) wait for a later pass")
    return p


def uploaded_count(ledger: dict[str, dict]) -> int:
    """How many distinct paths the ledger shows as sent."""
    return sum(1 for e in ledger.values() if e["upload"] is not None)


def _live_ids(path: str, ledger: dict[str, dict]) -> list[str]:
    entry = ledger.get(path)
    if not entry:
        return []
    return [i for i in entry["ids"] if i not in entry["deleted"]]


def _twin_rel(rel: str) -> str | None:
    twin = doc_scope.twin_of(Path(), rel)
    return None if twin is None else PurePosixPath(twin).as_posix()


def document_ids_for(rel: str, ledger: dict[str, dict]) -> list[str]:
    """The ids the ledger shows were sent for `rel` and for its twin, not yet deleted."""
    out = list(_live_ids(rel, ledger))
    twin = _twin_rel(rel)
    if twin and twin != rel:
        out += [i for i in _live_ids(twin, ledger) if i not in out]
    return out


def _file_sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        _LOGGER.info("memory_plan: cannot hash %s: %s", path, exc)
        return None


def _sent_paths(m: vr.Member, root: Path | None, shas: set[str]) -> list[str]:
    """The ledger paths a member reaches the bank through.

    Itself for a suffix in `SENT_AS_ITSELF`; otherwise its twin, and only when the twin on disk
    names one of `shas` as its source, so a same-stem twin of another original
    is never claimed.
    """
    if PurePosixPath(m.path).suffix.lower() in SENT_AS_ITSELF:
        return [m.path]
    twin = _twin_rel(m.path)
    if root is None or twin is None or not (root / twin).is_file():
        return []
    return [twin] if doc_scope.twin_source_sha(root / twin) in shas else []


def _ours(m: vr.Member, sha: str | None, upload: dict | None) -> bool:
    """True when `sha` is a revision of this member rather than unrelated content.

    Either the registry ever associated `sha` with the member, or `upload` (the
    ledger's last upload line for the path) was sent as a family's actual
    version, carrying a `header_fp`, and recorded exactly `sha`.
    """
    if sha is None:
        return False
    if sha in m.known_shas:
        return True
    return bool(upload and upload.get("header_fp") and upload.get("sha256") == sha)


def _deletable_ids(m: vr.Member, ledger: dict[str, dict], root: Path | None,
                   current: str | None = None) -> list[tuple[str, str]]:
    """(id, ledger path) pairs whose last upload is a revision of this member (`_ours`)."""
    out: list[tuple[str, str]] = []
    entry = ledger.get(m.path)
    upload = entry["upload"] if entry else None
    if upload and _ours(m, upload.get("sha256"), upload if upload.get("sha256") == current else None):
        out += [(i, m.path) for i in _live_ids(m.path, ledger)]
    twin = _twin_rel(m.path)
    if root is None or twin is None or twin == m.path:
        return out
    t_entry = ledger.get(twin)
    if not t_entry or not t_entry["upload"] or not (root / twin).is_file():
        return out
    source = doc_scope.twin_source_sha(root / twin)
    if source is None or not (source in m.known_shas or source == current):
        return out
    if _file_sha(root / twin) != t_entry["upload"].get("sha256"):
        return out
    return out + [(i, twin) for i in _live_ids(twin, ledger)]


def _dates(fam: vr.Family, root: Path | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if root is None:
        return out
    for aid, m in fam.members.items():
        try:
            mtime = (root / m.path).stat().st_mtime
        except OSError as exc:
            _LOGGER.info("memory_plan: no date for %s: %s", m.path, exc)
            continue
        out[aid] = datetime.fromtimestamp(mtime, timezone.utc).strftime("%Y-%m-%d")
    return out


def plan(state: vr.State, ledger: dict[str, dict], result: ScanResult, uploaded_count: int, *,
         max_deletes: int = MAX_DELETES, max_fraction: float = MAX_FRACTION, root: Path | None = None) -> Plan:
    """The memory plan for one pass. `ledger` is `index_ledger()` output.

    `root` enables everything that needs the disk: twins and header dates. With
    `root=None` only members sent as themselves are considered.
    """
    p = Plan()
    out_of_scope = set(result.out_of_scope)
    skipped = set(result.skipped)
    roles: dict[str, tuple[int, vr.Family, vr.Member]] = {}  # artifact_id -> (rank, family, member)
    for fam in state.families.values():
        if not fam.confirmed or fam.actual is None:
            continue
        for aid, m in fam.members.items():
            rank = 0 if aid == fam.actual else 2
            if aid not in roles or rank < roles[aid][0]:
                roles[aid] = (rank, fam, m)
        for aid, m in fam.proposed.items():
            if aid not in roles or roles[aid][0] > 1:
                roles[aid] = (1, fam, m)

    broken = {f.family_id for f in state.families.values()
              if f.confirmed and f.actual is not None and f.members[f.actual].state != "ok"}
    deletes: list[tuple[str, str]] = []
    for aid, (rank, fam, m) in roles.items():
        current = result.sha_of(m.path)
        shas = set(m.known_shas) | ({current} if current else set())
        paths = _sent_paths(m, root, shas)
        if rank == 1:
            for s in paths:
                p.hold.add(s)
                p.reasons[s] = f"proposed member of family {fam.family_id}, not decided"
            continue
        if rank == 2 and fam.family_id in broken:
            for s in paths or [m.path]:
                p.hold.add(s)
                p.reasons[s] = f"history of family {fam.family_id}, held while its actual version is not available"
            continue
        if m.state == "unavailable":
            p.skip.update(paths)
            if rank == 0:
                p.notes.append(f"error: actual version of family {fam.family_id} is unavailable at {m.path}; "
                               "no replacement is chosen automatically")
            continue
        if m.state == "missing":
            if rank == 0:
                p.notes.append(f"error: actual version of family {fam.family_id} is missing at {m.path}; its "
                               "documents are deleted, its history is held, and no replacement is chosen")
            p.skip.update(paths)
            if aid not in out_of_scope and result.sha_of(m.path) is None and m.path not in skipped:
                deletes += _deletable_ids(m, ledger, root)
            continue
        if rank == 0:
            header = header_for(fam, _dates(fam, root))
            for s in paths:
                p.upload[s] = header
                p.fingerprints[s] = header_fp(header, fam.actual_sha or "")
            continue
        # history
        entry = ledger.get(m.path)
        if current is not None and not _ours(m, current, entry["upload"] if entry else None):
            continue  # new content at a reused path: sent as an ordinary file, nothing deleted
        if aid in out_of_scope or m.path in skipped:
            why = "outside this scan's scope" if aid in out_of_scope else "changed while scanned"
            for s in paths or [m.path]:
                p.hold.add(s)
                p.reasons[s] = f"history of family {fam.family_id}, {why}"
            continue
        p.skip.update(paths)
        if current is not None:
            deletes += _deletable_ids(m, ledger, root, current)

    seen: set[str] = set()
    sent_or_held = set(p.upload) | p.hold | skipped
    for doc_id, path in deletes:
        if path in sent_or_held:
            p.notes.append(f"not deleting {doc_id}: its path {path} is sent or held in this same pass")
            continue
        if doc_id not in seen:
            seen.add(doc_id)
            p.delete.append(doc_id)
            p.delete_path[doc_id] = path
    if not result.complete:
        if p.delete:
            p.notes.append(f"scan incomplete: {len(p.delete)} planned deletion(s) dropped this pass")
        p.delete, p.delete_path = [], {}
    n = len(p.delete)
    if n and (n > max_deletes or n > max_fraction * uploaded_count):
        p.blocked = (f"{n} deletions exceed the guard; review them with `hindsight-sync.py blocked list`, "
                     "then run `hindsight-sync.py blocked delete --confirm <N:H>`")
        p.delete, p.delete_path = [], {}
    return p
