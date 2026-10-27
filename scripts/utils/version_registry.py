"""Append-only event log of document version families, and its replay.

Every writer takes `locked()` before touching the file; `append()` fills in
`schema_version`, `event_id`, `seq` and `ts` on the caller's event, then writes
one JSON line. `replay()` is pure: it never reads the disk, never calls a
model, and never writes anything. It folds a list of events into a `State` and
raises `RegistryCorrupt` the moment an invariant breaks, so `append()` can run
the replay on the prospective new event BEFORE it is written and refuse to
write it at all when the log would come out corrupt.

`locked()` is re-entrant within one process: a module-level depth counter keyed
by the resolved lock path lets a caller already holding the lock call
`append()` (which itself takes `locked()`) without deadlocking on its own
flock. Only the outermost `locked()` call in a process actually opens the lock
file and takes or releases the flock; the flock itself is still one per
process across the whole call stack, and still cooperates correctly with a
second process taking the same lock, because flock is scoped to the open file
description and the depth counter only ever changes what THIS process does.

Two structural checks below go beyond the five invariants their reasoning
starts from: one guard raises `RegistryCorrupt` for any family-scoped kind
(`family_confirmed`, `member_proposed`, `member_confirmed`, `member_removed`,
`decided`, `summary`) naming a `family_id` that is missing or that no
`family_proposed` ever declared, and a second raises it for a
`member_confirmed` naming an artifact that was never proposed. Both replace
what would otherwise be a `replay()` crash on a missing key or a `None`
attribute access. A hand-written log can be wrong in more ways than the five
invariants below name, and a reader that crashes on the sixth way is not safer
than one that names it and refuses.

`family_dismissed` (2026-10-12) takes an UNCONFIRMED family out of play without
saying anything about its content: a proposal left with fewer than two members,
or the emptied side of a merge. It moves the family from `State.families` to
`State.dismissed`, so no reader sees it again and its `family_id` can never be
declared a second time; dismissing a confirmed family, or naming a dismissed
one in any later event, is `RegistryCorrupt`. A rejection ("these are not
versions") is a different claim and stays the `rejected` event's alone.

`append()` also repairs the registry's last line before writing, in one of
three ways (`_repair_torn_tail()`, called before either the replay-check or
the write). A complete, valid last line that already ends in a newline is left
untouched. A complete, valid last line MISSING only its trailing newline gets
that newline appended in place; it is a real event and is never moved or
truncated. Only a last line that does not parse as JSON at all is a genuine
tear: a writer cut off mid-line leaves bytes `read_events()` would otherwise
tolerate as a dropped last line, and a second `append()` would glue its own
line onto them, turning a tolerated warning into a permanently corrupt middle
line the moment a third event is appended. That fragment is moved verbatim to
a `<registry>.torn` sidecar (written before anything is removed from the
registry) and the registry is truncated to its last complete line.
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve()
if str(_HERE.parents[2]) not in sys.path:
    sys.path.insert(0, str(_HERE.parents[2]))

from scripts.utils.data_path import data_path  # noqa: E402

#: The schema version every stored event carries. A registry declaring anything
#: else is refused rather than read optimistically.
SCHEMA_VERSION = 1

#: The event types `replay()` understands. Anything else fails `read_events()`
#: closed, on the reasoning that an unknown type is more likely a future schema
#: this code has not caught up with than a typo worth silently ignoring.
KINDS = frozenset({
    "family_proposed", "family_confirmed", "member_proposed", "member_confirmed",
    "member_removed", "decided", "rejected", "relocated", "unavailable", "missing",
    "available", "summary", "family_dismissed",
})

#: Every kind whose handling below reads `fam` (as opposed to `family_proposed`,
#: which CREATES it; `rejected`, which carries no family_id at all; and the four
#: state kinds, which search every family by artifact_id instead of by
#: family_id). ONE guard checks this set before the kind dispatch, so a missing
#: or unknown `family_id` on any of them raises `RegistryCorrupt` in one place
#: rather than crashing on a `None` attribute access in each branch separately.
FAMILY_SCOPED_KINDS = frozenset({
    "family_confirmed", "member_proposed", "member_confirmed", "member_removed",
    "decided", "summary", "family_dismissed",
})


#: The keys every stored event carries, and the keys each kind needs on top of
#: them. `_validate()` checks both before `replay()` reads a single field, so a
#: hand-edited or truncated-but-parseable line is `RegistryCorrupt`, never a
#: `KeyError` or `AttributeError` escaping to a caller that only expects the
#: former.
_REQUIRED_KEYS = ("schema_version", "type", "seq")
_KIND_KEYS: dict[str, tuple[str, ...]] = {
    "family_proposed": ("family_id", "members"),
    "family_confirmed": ("family_id",),
    "member_proposed": ("family_id", "member"),
    "member_confirmed": ("family_id", "artifact_id"),
    "member_removed": ("family_id", "artifact_id"),
    "decided": ("family_id", "artifact_id", "sha256"),
    "rejected": ("shas",),
    "relocated": ("artifact_id", "path"),
    "unavailable": ("artifact_id",),
    "missing": ("artifact_id",),
    "available": ("artifact_id",),
    "summary": ("family_id", "from_sha", "to_sha", "text"),
    "family_dismissed": ("family_id", "reason"),
}
_MEMBER_KEYS = ("artifact_id", "path", "sha256")

#: Seconds between two non-blocking attempts at the registry flock when
#: `locked()` is given a `timeout`.
LOCK_RETRY_INTERVAL = 0.25


class RegistryCorrupt(Exception):
    """The event log, or the event about to join it, breaks an invariant."""


class LockTimeout(TimeoutError):
    """`locked(timeout=...)` could not take the registry flock in time."""


@dataclass
class Member:
    artifact_id: str
    path: str
    sha256: str
    state: str = "ok"  # "ok" | "unavailable" | "missing"
    #: Every SHA-256 the registry ever associated with this artifact: the one it
    #: was proposed with, each `relocated` hash and each `decided` hash. The
    #: memory plan reads it to tell a member edited in place (still ours) from
    #: unrelated new content at a reused path.
    known_shas: set[str] = field(default_factory=set)
    #: The `ts` of the latest event that saw this member present at `path`: its
    #: proposal, a `decided`, a `relocated` or an `available`. `doc_scan` reads
    #: it as the lower bound a move's ctime must beat when a member already
    #: absent is followed to a path that came into scope late. None when no
    #: such event carried a `ts`.
    seen_at: str | None = None

    def __post_init__(self) -> None:
        self.known_shas.add(self.sha256)


@dataclass
class Family:
    family_id: str
    confirmed: bool = False
    members: dict[str, Member] = field(default_factory=dict)
    proposed: dict[str, Member] = field(default_factory=dict)
    actual: str | None = None
    actual_sha: str | None = None
    summaries: dict[tuple[str, str], str] = field(default_factory=dict)


@dataclass
class State:
    families: dict[str, Family] = field(default_factory=dict)
    rejections: set[frozenset[str]] = field(default_factory=set)
    generation: int = 0
    #: Families a `family_dismissed` event took out of play (2026-10-12): a
    #: proposal left with fewer than two members, or one merged into another.
    #: Kept only so a `family_id` is never declared twice; every reader of
    #: `families` stops seeing them, and any later event naming one is corrupt.
    dismissed: dict[str, Family] = field(default_factory=dict)


def is_rejected(fam: Family, state: State) -> bool:
    """True when every revision `fam` holds sits inside one rejection.

    Such a family has nothing left to decide: `reject <family>` recorded its
    revisions as "not versions of each other" and only that rejection governs
    them now. A family with no revisions at all is not rejected, it is empty.
    """
    shas = {m.sha256 for m in list(fam.members.values()) + list(fam.proposed.values())}
    return bool(shas) and any(shas <= rej for rej in state.rejections)


def registry_path() -> Path:
    """The one registry file, resolved by logical name, never by a built path."""
    return data_path("version-registry")


#: One flock per resolved lock path, shared by every writer in THIS process.
#: Keyed by the resolved path string rather than the `Path` object, because two
#: `Path` instances naming the same file are equal but a dict keyed on the
#: unresolved form would not catch `reg.jsonl` and `./reg.jsonl` as the same
#: lock. Never cleared explicitly; an entry is removed the moment its count
#: returns to zero, so the dict never grows past the number of locks a live
#: call stack is actually holding.
_LOCK_DEPTH: dict[str, int] = {}


@contextmanager
def locked(path: Path | None = None, timeout: float | None = None):
    """One flock per resolved lock path, re-entrant within this process.

    The outermost call opens the `.lock` file and takes an exclusive flock; a
    nested call for the SAME resolved path only bumps the depth counter and
    yields, so `append()` (which takes this lock itself) can be called from
    inside a block that already holds it. A second PROCESS taking the same
    lock still blocks on the real flock, because the depth counter is
    process-local and the flock itself is untouched by it.

    `timeout=None` (the CLI's default) waits as long as it takes. A number
    makes the outermost call try a non-blocking flock every
    `LOCK_RETRY_INTERVAL` seconds and raise `LockTimeout` once that many
    seconds have passed, so an unattended caller never hangs behind a human
    holding the lock. A nested call never waits, so it ignores `timeout`.
    """
    p = (path or registry_path()).with_suffix(".lock")
    key = str(p.resolve())
    depth = _LOCK_DEPTH.get(key, 0)
    fh = None
    if depth == 0:
        p.parent.mkdir(parents=True, exist_ok=True)
        fh = p.open("a")
        try:
            _take_flock(fh, timeout)
        except Exception:  # the fd must not leak if taking the lock itself fails
            fh.close()
            raise
    _LOCK_DEPTH[key] = depth + 1
    try:
        yield
    finally:
        remaining = _LOCK_DEPTH[key] - 1
        if remaining == 0:
            del _LOCK_DEPTH[key]
        else:
            _LOCK_DEPTH[key] = remaining
        if fh is not None:
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()


def _take_flock(fh, timeout: float | None) -> None:
    """Block on the flock, or retry it non-blocking until `timeout` seconds pass."""
    if timeout is None:
        fcntl.flock(fh, fcntl.LOCK_EX)
        return
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            left = deadline - time.monotonic()
            if left <= 0:
                raise LockTimeout(f"registry lock {fh.name} still held after {timeout} s") from None
            time.sleep(min(LOCK_RETRY_INTERVAL, left))


def read_events(path: Path | None = None) -> tuple[list[dict], list[str]]:
    """The stored events, and warnings for anything tolerated rather than fixed.

    A last line that will not parse as JSON is a writer caught mid-append; it is
    dropped and reported as a warning, never raised. A middle line that will not
    parse, any line that parses but is not a JSON object (wherever it sits: a
    parseable line is not a torn write), and any line declaring an unknown
    `schema_version` or `type` mean the file itself is not trustworthy, and
    raise `RegistryCorrupt` instead of reading around the damage. Missing keys
    are `replay()`'s to refuse (`_validate`).
    """
    p = path or registry_path()
    if not p.exists():
        return [], []
    lines = p.read_text(encoding="utf-8").splitlines()
    events: list[dict] = []
    warnings: list[str] = []
    last_index = len(lines) - 1
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError as exc:
            if i == last_index:
                warnings.append(f"dropped an unparseable last line: {exc}")
                continue
            raise RegistryCorrupt(f"line {i + 1} of {p} is not JSON: {exc}") from exc
        if not isinstance(ev, dict):
            raise RegistryCorrupt(f"line {i + 1} of {p} is {type(ev).__name__}, not an event object")
        if ev.get("schema_version") != SCHEMA_VERSION or ev.get("type") not in KINDS:
            raise RegistryCorrupt(
                f"line {i + 1} of {p}: unknown schema_version or type {ev.get('type')!r}")
        events.append(ev)
    return events, warnings


def _repair_torn_tail(p: Path) -> str | None:
    """Repair the registry's last line before anything writes past it.

    Runs at the START of `append()`, inside the caller's `locked()` block.
    Three cases, decided on the raw bytes:

    (a) The file ends with `\\n` and its last non-empty line parses as JSON:
        nothing is wrong. No-op.
    (b) The last non-empty line parses as JSON but the file does NOT end with
        `\\n`: a writer finished the line but was cut off before the newline.
        The line is a complete, valid record and must not be touched; only a
        `\\n` is appended, in place. Nothing moves to `.torn`, nothing is
        truncated.
    (c) The last non-empty line does not parse as JSON at all, whether or not
        it ends with `\\n`: a writer was cut off mid-line. Left alone, the
        next `append()` would glue its own JSON line onto these bytes, turning
        a last-line warning `read_events()` would otherwise tolerate into a
        permanently corrupt MIDDLE line the moment a THIRD event is appended.
        So the fragment is moved verbatim to `<registry>.torn` (written before
        anything is removed from the registry, so the record is never
        destroyed, only relocated), and the registry is truncated, with
        `os.truncate`, to the byte offset of the last complete, parseable
        line.

    Returns a message describing what happened when (b) or (c) fired, else
    `None`.
    """
    if not p.exists():
        return None
    raw = p.read_bytes()
    if not raw:
        return None
    ends_clean = raw.endswith(b"\n")
    body = raw[:-1] if ends_clean else raw
    lines = body.split(b"\n")
    last = lines[-1]
    if not last.strip():
        return None  # a blank tail line is not a tear
    try:
        json.loads(last)
    except json.JSONDecodeError:
        pass
    else:
        if ends_clean:
            return None  # case (a): complete and parseable, nothing to do
        with p.open("ab") as fh:  # case (b): a valid line missing only its newline
            fh.write(b"\n")
        return f"version_registry: appended a missing trailing newline to {p}"
    # case (c): the last line does not parse at all, so it is a real tear
    good = b"\n".join(lines[:-1])
    good = good + b"\n" if good else b""
    torn_bytes = raw[len(good):]
    torn_path = p.with_name(p.name + ".torn")
    with torn_path.open("ab") as fh:  # write the fragment before truncating: never destroy a record
        fh.write(torn_bytes)
    os.truncate(p, len(good))
    return f"version_registry: repaired a torn tail, {len(torn_bytes)} byte(s) moved to {torn_path}"


def append(event: dict, *, path: Path | None = None) -> dict:
    """Store one event, after proving the log stays sound with it added.

    Repairs a torn tail left by a crashed writer FIRST (see
    `_repair_torn_tail`), then replays every existing event plus this one
    BEFORE writing anything; a `RegistryCorrupt` from that replay propagates to
    the caller with the file untouched. The lock covers the repair, the read,
    the replay and the write as one unit, so two writers can never interleave
    any of it.
    """
    p = path or registry_path()
    with locked(p):
        warning = _repair_torn_tail(p)
        if warning:
            print(warning, file=sys.stderr)
        events, _ = read_events(p)
        for ev in events:  # before `seq` is read below: a malformed line is RegistryCorrupt, not KeyError
            _validate(ev)
        next_seq = (events[-1]["seq"] + 1) if events else 1
        stored = {
            "schema_version": SCHEMA_VERSION,
            "event_id": uuid.uuid4().hex,
            "seq": next_seq,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "actor": "helm",
            **event,
        }
        replay(events + [stored])  # raises RegistryCorrupt; nothing below runs if it does
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(stored, ensure_ascii=False) + "\n")
    return stored


def _member(d: dict, ts: str | None = None) -> Member:
    return Member(d["artifact_id"], d["path"], d["sha256"], d.get("state", "ok"), seen_at=ts)


def _validate(ev) -> None:
    """`RegistryCorrupt` unless `ev` is an object carrying every key its kind reads."""
    if not isinstance(ev, dict):
        raise RegistryCorrupt(f"an event is {type(ev).__name__}, not an object")
    missing = [k for k in _REQUIRED_KEYS if k not in ev]
    if missing:
        raise RegistryCorrupt(f"event {ev.get('event_id', '?')!r} lacks {', '.join(missing)}")
    if not isinstance(ev["seq"], int) or isinstance(ev["seq"], bool):
        raise RegistryCorrupt(f"event {ev.get('event_id', '?')!r} has a non-integer seq {ev['seq']!r}")
    kind = ev["type"]
    if kind not in KINDS:
        raise RegistryCorrupt(f"event {ev.get('event_id', '?')!r} has unknown type {kind!r}")
    missing = [k for k in _KIND_KEYS[kind] if k not in ev]
    if missing:
        raise RegistryCorrupt(f"event {ev.get('event_id', '?')!r} ({kind}) lacks {', '.join(missing)}")
    members = (ev["members"] if kind == "family_proposed"
               else [ev["member"]] if kind == "member_proposed" else [])
    if kind == "family_proposed" and not isinstance(members, list):
        raise RegistryCorrupt(f"event {ev.get('event_id', '?')!r} ({kind}) members is not a list")
    for m in members:
        if not isinstance(m, dict) or any(k not in m for k in _MEMBER_KEYS):
            raise RegistryCorrupt(f"event {ev.get('event_id', '?')!r} ({kind}) has a member without "
                                  f"{', '.join(_MEMBER_KEYS)}")
    if kind == "rejected" and not isinstance(ev["shas"], list):
        raise RegistryCorrupt(f"event {ev.get('event_id', '?')!r} (rejected) shas is not a list")


def replay(events: list[dict]) -> State:
    """Fold `events` into a `State`, in `seq` order. Pure: no disk, no model.

    Raises `RegistryCorrupt` when the resulting state would violate an
    invariant: an artifact in more than one CONFIRMED family, a family's
    `actual` naming something other than one of its confirmed members, an
    event naming a family no `family_proposed` ever declared, a
    `member_confirmed` for an artifact that was never proposed, a second
    `family_proposed` for a `family_id` already declared (it would silently
    replace the first family and everything decided about it), one path held
    by two confirmed families, or an event that is not an object or lacks a
    key its kind reads (`_validate`, checked for every event before any is
    folded), a dismissal of a confirmed family, or any event naming a
    dismissed one.
    """
    for ev in events:
        _validate(ev)
    st = State()
    for ev in sorted(events, key=lambda e: e["seq"]):
        kind = ev["type"]
        fid = ev.get("family_id")
        if kind == "family_proposed":
            if fid in st.families or fid in st.dismissed:
                raise RegistryCorrupt(
                    f"event {ev.get('event_id', '?')!r} proposes family {fid!r} a second time")
            st.families[fid] = Family(
                fid, proposed={m["artifact_id"]: _member(m, ev.get("ts")) for m in ev["members"]})
            st.generation = ev["seq"]
            continue
        fam = st.families.get(fid) if fid is not None else None
        if kind in FAMILY_SCOPED_KINDS and fam is None:
            gone = " (dismissed)" if fid in st.dismissed else ""
            raise RegistryCorrupt(
                f"event {ev.get('event_id', '?')!r} ({kind}) names a missing or unknown family {fid!r}{gone}")
        if kind == "family_dismissed":
            if fam.confirmed:
                raise RegistryCorrupt(
                    f"event {ev.get('event_id', '?')!r} dismisses confirmed family {fid!r}")
            st.dismissed[fid] = st.families.pop(fid)
        elif kind == "family_confirmed":
            fam.confirmed = True
            fam.members.update(fam.proposed)
            fam.proposed.clear()
        elif kind == "member_proposed":
            fam.proposed[ev["member"]["artifact_id"]] = _member(ev["member"], ev.get("ts"))
        elif kind == "member_confirmed":
            aid = ev["artifact_id"]
            member = fam.proposed.pop(aid, None)
            if member is None:
                raise RegistryCorrupt(
                    f"member_confirmed for {aid!r} was never proposed in family {fid!r}")
            fam.members[aid] = member
        elif kind == "member_removed":
            aid = ev["artifact_id"]
            fam.members.pop(aid, None)
            fam.proposed.pop(aid, None)
            if fam.actual == aid:
                fam.actual = fam.actual_sha = None
        elif kind == "decided":
            fam.actual, fam.actual_sha = ev["artifact_id"], ev["sha256"]
            decided = fam.members.get(ev["artifact_id"]) or fam.proposed.get(ev["artifact_id"])
            if decided is not None:
                decided.known_shas.add(ev["sha256"])
                decided.seen_at = ev.get("ts") or decided.seen_at
        elif kind == "rejected":
            st.rejections.add(frozenset(ev["shas"]))
        elif kind in ("relocated", "unavailable", "missing", "available"):
            for f in st.families.values():
                for m in list(f.members.values()) + list(f.proposed.values()):
                    if m.artifact_id == ev["artifact_id"]:
                        if kind == "relocated":
                            m.path = ev["path"]
                            m.sha256 = ev.get("sha256", m.sha256)
                            m.known_shas.add(m.sha256)
                            # found at its new path, so present: a member a
                            # pass had already marked absent is available again
                            m.state = "ok"
                            m.seen_at = ev.get("ts") or m.seen_at
                        else:
                            m.state = "ok" if kind == "available" else kind
                            if kind == "available":
                                m.seen_at = ev.get("ts") or m.seen_at
        elif kind == "summary":
            fam.summaries[(ev["from_sha"], ev["to_sha"])] = ev["text"]
        st.generation = ev["seq"]
    _check(st)
    return st


def _check(st: State) -> None:
    """The invariants a replayed state must hold, or `RegistryCorrupt`."""
    owner: dict[str, str] = {}
    path_owner: dict[str, str] = {}
    for f in st.families.values():
        if not f.confirmed:
            continue
        for aid, m in f.members.items():
            if aid in owner:
                raise RegistryCorrupt(
                    f"artifact {aid} is in confirmed families {owner[aid]!r} and {f.family_id!r}")
            owner[aid] = f.family_id
            if path_owner.get(m.path, f.family_id) != f.family_id:
                raise RegistryCorrupt(
                    f"path {m.path} is in confirmed families {path_owner[m.path]!r} and {f.family_id!r}")
            path_owner[m.path] = f.family_id
        if f.actual is not None and f.actual not in f.members:
            raise RegistryCorrupt(
                f"family {f.family_id}: actual {f.actual!r} is not a confirmed member")
