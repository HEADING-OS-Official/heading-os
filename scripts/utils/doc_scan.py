"""One scan pass: what is where, what moved, and what merely looks gone.

`scan()` walks the scope roots `scripts.utils.doc_scope` names (or an invented
set a self-test passes) and returns a `ScanResult`: every in-scope file's
current SHA-256, whether the pass saw every root and read every file it tried
to read, and the things a single walk can notice about itself that a later
reader cannot reconstruct: a file whose bytes changed while the walk was
touching it, two files that currently carry the same content, and a
directory reached only through a symlink (this workspace forbids symlinks,
and a walk that quietly skips one hides whatever sits behind it).

`availability_events()` turns one `ScanResult` plus the registry's own
replayed `State` into the events a caller may then hand to
`version_registry.append()`. It never writes anything and never reads the
registry file; it is pure, exactly like `version_registry.replay()`, so its
correctness can be checked on invented members with no disk beyond a temp
directory.

## Gate on `complete`, never on whether `errors` is empty

`errors` is DIAGNOSTIC, not a verdict. Every path that appends to `errors`
below also sets `complete` False in the same breath, including a cache write
that fails (see the cache section): there is currently no case where `errors`
is non-empty while `complete` stays `True`. That is a fact about today's code,
not a guarantee this docstring is making about tomorrow's: a future
non-fatal diagnostic could reintroduce the split, and the contract callers
must hold to either way is unchanged. `complete` is the one field a caller
gates destructive decisions on; `errors` is what a human reads to find out
why.

## Why a scan owns its own completeness, rather than trusting the caller's try/except

The spec this module is built against is explicit that a scan which did not see
everything must plan nothing destructive from what it did not see: "a scan that
fails halfway ... must plan zero deletions, even if many files look absent."
That means `complete` cannot be a bookkeeping nicety set by whoever calls
`scan()`; it has to be TRUE exactly when every scope root existed, every
directory under it could be listed, and every file this pass decided to hash
could be read. A missing scope root, a symlinked directory or path component,
any exception from resolving the scope roots themselves, an `OSError` from
`os.walk`'s own `onerror` callback, a `stat()` that raises, a read that raises
inside the hash loop, and a cache that could not be written back all set it,
and none of them raises out of `scan()` itself: the function's whole job is to
turn "something went wrong somewhere in this tree" into one boolean plus a
list of what happened, so `availability_events()` can refuse to say anything
at all rather than trying to tell a real absence from a side effect of the
failure.

## Why a changed-during-the-walk file is neither present nor absent

A file whose (size, mtime_ns) differ between the stat taken before hashing and
the one taken after is being written while this pass is reading it; its
just-computed digest describes bytes that may already be stale by the time the
function returns. Reporting it as absent would tell `availability_events()`
that a document vanished when it did nothing but get edited during an unlucky
second, and reporting a possibly-wrong digest as its current one would let a
relocation or a duplicate decision be made on bytes the file no longer holds.
So it goes into `ScanResult.skipped` instead, is left out of both
`ScanResult.files` and any hash a relocation could trust, and
`availability_events()` treats a path in `skipped` exactly like a path in
`files`: present, its state left alone (or, if the member was previously
unavailable or missing, reported available again), because it is neither an
absence nor a change worth trusting. Its just-computed (and possibly stale)
digest is kept ONLY in `ScanResult.skipped_shas`, never in `files`, so a
relocation decision can still refuse to treat that digest as unambiguous (see
below) without ever treating the file itself as a confirmed match for
anything. The cache still records the path, with a `null` hash instead of a
real one (see the cache section below): a skipped file is not gone, and
forgetting it existed would make it look like a brand new path on the very
next pass.

## The cache: one file's worth of memory across passes, and what it must never lose

The cache is `{"written_at": <int, time.time_ns() at the write>, "files":
{rel: [size, mtime_ns, sha256_or_null]}}`. An old-format cache (the flat
`{rel: [...]}` shape this module used before this file carried a `written_at`)
or anything that does not parse as this exact shape is treated as NO usable
cache at all, never as an empty one: `ScanResult.cache_paths_at_start` and
`ScanResult.cache_written_at` are both `None`, and no relocation is ever
proposed on that pass, per the rule below.

Three failure modes were MEASURED to produce a false `relocated` onto a
long-existing file before this section's rules were enforced, and each is
named here so a future change does not reopen it:

(c) An INCOMPLETE pass must never simply overwrite the cache with only the
    paths it managed to see this time: a path hidden behind whatever made the
    pass incomplete (a locked directory, an unreadable file) would silently
    drop out of the persisted cache, and the very next pass would see it as
    brand new. `scan()` instead persists the UNION of the previous cache's
    keys and the keys this pass actually saw, and never removes a key the
    previous cache had that this pass did not confirm one way or the other.
    Only a COMPLETE pass is trusted to replace the cache outright, because
    only a complete pass has looked at everything a dropped key could mean.
(d) A cache write that FAILS must not leave the pass looking `complete=True`:
    the disk copy of the cache is now stale relative to what this pass
    actually found, and a later pass trusting that stale copy's key set could
    misjudge which paths are new. So a failed write sets `complete=False` for
    THIS pass (with the failure recorded in `errors`), which per the gate
    above already means `availability_events()` proposes nothing from it.
(e) A SKIPPED file (changed mid-walk) must still get a cache entry, just with
    a `null` hash rather than a real one: leaving it out of the cache entirely
    made it look brand new to the very next pass, exactly like case (c) but
    self-inflicted rather than caused by an external failure.

**A known, accepted limitation of case (c), stated rather than left to be
discovered.** A genuine move that happens in the SAME window as an incomplete
pass, one where the old path had already vanished and the new path had
already appeared before that incomplete pass ran, is learned by (c) as an
ORDINARY previously-known path, not as a fresh relocation: the union merge
folds the new path into the cache's key set the moment ANY pass (complete or
not) sees it, so the very next pass no longer finds it "new" even though
nothing had confirmed the move as unambiguous yet. The member then goes
`unavailable` (or `missing`, on a second consecutive absence) instead of
`relocated`. This is the SAFE direction, not a silent loss: the member is
never deleted or assumed gone for good on the strength of a guess, and the
version detector (`doc_detector`), working from the same cache and duplicate
information, can still propose the very same move for a human to confirm. The alternative,
treating a merely-not-yet-confirmed path as a confident relocation, is the
false positive this whole section exists to prevent.

**Defence in depth: a relocation candidate's own ctime, not just its
newness to the cache's key set.** The path-based "new since the cache" check
above answers "was this a key of the cache we loaded", which can be fooled by
anything that resets or narrows that key set (a cache that was cleared, a
scope root that only now started covering this file). A rename or move
updates a file's ctime (MEASURED empirically: `os.rename` leaves `mtime`
untouched and bumps `ctime`); a file that has simply sat where it always was
does not. So an unambiguous relocation ALSO requires the candidate's
`st_ctime_ns` (`ScanResult.file_ctimes`) to be strictly greater than the
loaded cache's own `written_at` (`ScanResult.cache_written_at`): a file whose
metadata predates the last time this pass's cache was written cannot have
just moved there, whatever the key-set check alone would have said.

## Why relocation needs five things to agree, not one

A member whose recorded path is absent this pass is `relocated` only when ALL
of the following hold, over the one SHA-256 it was last known to carry:

1. The member is the ONLY absent member carrying that SHA-256 this pass
   (`absent_by_sha[m.sha256]` has exactly one entry: itself). Two members
   recorded with identical content are exactly the case a single "one
   candidate file" check cannot resolve: both would each look like an
   unambiguous match for the same new file, and at most one of them can
   actually have moved there.
2. Exactly one in-scope file this pass saw carries that SHA-256 (an
   archive-shaped file below a scope root counts, `archive_candidates`), and that
   path is not any member's own recorded path (a path already owned by some
   member is that member being present, not a new location for a different
   one).
3. No file in `ScanResult.skipped` carries that SHA-256 either, judged by the
   digest computed for it before its stat moved (`skipped_shas`): a file mid
   edit at the exact moment of this pass is not proof of absence of a second
   candidate, only of the walk's own bad timing, so it must not be treated as
   though it were not there.
4. The one candidate path was NOT already a key of the cache loaded at the
   START of this pass (`ScanResult.cache_paths_at_start`): "relocated" means a
   new path appeared in the same pass the old one vanished, and a path this
   scan has known about since before is a long-standing file that happens to
   share content with something that just went missing, not a destination it
   just moved to. With no usable previous cache at all (the very first scan
   of a tree, or one whose cache was missing, corrupt, or in the old format),
   NOTHING this pass sees can be shown to be new, so no relocation is ever
   proposed on it; the member instead takes the ordinary unavailable route,
   for a later, explicit proposal to pick up once there is a baseline to
   compare against.
5. The candidate's own `st_ctime_ns` is strictly greater than the loaded
   cache's `written_at` (see "defence in depth" above): condition 4 alone can
   be fooled by anything that resets the cache's key set; a real move always
   touches the file's own metadata, and a long-standing file's ctime never
   moves on its own.

**A member already absent is followed late (2026-10-12, Part C pilot).**
Conditions 4 and 5 compare the candidate with the scan cache, so they can
only ever see a move whose destination was in scope on the pass the source
vanished. MEASURED 2026-10-12: five members moved into a tree the scope did
not yet cover went `unavailable` and then `missing`, and once the scope
widened their new paths were already older than the cache and already its
keys, so they stayed `missing` for good. For a member whose recorded state is
already `unavailable` or `missing` (a complete pass has proved the old path
gone), conditions 4 and 5 may instead be met together by ONE registry-side
test: the candidate's `st_ctime_ns` is strictly later than the member's
`seen_at`, the `ts` of the latest registry event that saw it present at its
recorded path (its proposal, a `decided`, a `relocated`, an `available`).
What 4 and 5 guard against is a long-standing file that happens to share the
content; such a file was last touched before the member was last seen at its
own path, so its ctime is older than `seen_at` and it is still refused. A
member with no `seen_at` never takes this route. Conditions 1 to 3 are
unchanged and still required.

Two or more members sharing a SHA-256, two or more files sharing a SHA-256, or
a shared SHA-256 that also touches a skipped file are all recorded once in
`ScanResult.duplicates` (built from `files` alone, regardless of any of this),
and every member whose SHA-256 fails one of the five conditions above falls
through to the ordinary absent/unavailable/missing handling instead of ever
being considered for a move.

## Why `prev_absent` is a parameter and not something this module re-derives

`Member.state` already carries "ok" / "unavailable" / "missing", set by the
very `unavailable` / `missing` / `available` events this function proposes,
once a caller has appended them (`version_registry.replay()`'s handling of
those four kinds). But a member whose state is already "missing" must stay
silently missing without a new event, while a member whose state is
"unavailable" must become "missing" on its next absence: those are two
different responses to the identical fact "still absent", and only the
CALLER, having appended or not appended last pass's proposed events, knows
which artifacts are currently in the first bucket. `prev_absent` is exactly
that bucket, handed in rather than re-scanned out of `state`, and the returned
set is the same bucket updated for whatever this pass decided: an artifact
newly absent joins it, one that just became `missing` or `available` or
`relocated` leaves it. On an incomplete scan the function changes nothing and
returns `prev_absent` back unchanged, because nothing it saw this pass can be
trusted enough to update the bucket with. An incomplete pass in between two
complete ones does not reset anything either, for the identical reason: it
neither confirms nor denies an absence, so the two-consecutive-complete-passes
count for "missing" is a count of complete passes, silently unaffected by
however many incomplete ones fall between them.

## Why a member outside this pass's own scope roots gets nothing

A member's recorded path can stop being in scope without the file itself ever
moving: the scope roots the workspace tracks can change between passes,
carrying a member's old path out from under them. Reporting such a member
`unavailable` (or, worse, `missing`) would say a document vanished when this
pass in fact never looked at that part of the tree at all, so it is set aside
into `ScanResult.out_of_scope` (by artifact_id) with no event whatsoever,
checked with `doc_scope.in_scope()` against the concrete roots THIS pass used
(`ScanResult.roots`), never against whatever `roots=None` would resolve to at
some later, possibly different, moment. `availability_events()` REPLACES
`result.out_of_scope` with a freshly built list on every call rather than
appending to whatever was already there, so calling it twice on the same
`ScanResult` (a retry, a second look) reports the same members once each,
never twice.

## A confirmed member whose old path left the scope (2026-10-14, HOS-178)

The rule above also kept a member out of reach when its FILE moved: the old
path left the scope with the move and the new one was not new to the cache,
so no route found it. MEASURED 2026-10-14 on a copy of the live registry: a
data move left confirmed members at old paths outside every scope root,
with no file there; the pass emitted no event for any of them, and nothing
ever would.

So `scan(members=...)` `lstat`s each confirmed member path outside the scope
(`ScanResult.gone_out_of_scope` holds those found absent; any other `OSError`
makes the pass incomplete), and `availability_events` follows such a member to
a new path when conditions 1 to 3 hold (now counting every absent member, in
scope or not, for condition 1, and every member's recorded path, in scope or
not, for condition 2) and the candidate's ctime is strictly later than the
member's `seen_at`, the test "followed late" uses. A member gone from outside
the scope has never been marked absent, so that test is its only proof of a
move; one with no `seen_at` is not followed. Anything short of all of that
stays out of scope with no event, exactly as before: never `unavailable`,
never `missing`. A proposed (not yet confirmed) member is never followed this
way.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Iterable

_HERE = Path(__file__).resolve()
if str(_HERE.parents[2]) not in sys.path:
    sys.path.insert(0, str(_HERE.parents[2]))

from scripts.utils import doc_scope  # noqa: E402
from scripts.utils import version_registry as vr  # noqa: E402
from scripts.utils.data_path import data_path  # noqa: E402
from scripts.utils.paths import DataRootError  # noqa: E402

__all__ = ["ScanResult", "default_cache_path", "scan", "availability_events", "confirmed_member_paths"]

#: Streaming hash chunk size. Never read a whole document into memory to hash
#: it; a multi-hundred-megabyte PDF is exactly the kind of file this registry
#: tracks.
_CHUNK_BYTES = 1 << 20

#: A trustworthy cached SHA-256 and nothing else: exactly 64 lowercase hex
#: digits. A cache is a plain JSON file nothing stops a hand edit, a merge, or
#: a half-written `.tmp` recovery from putting something else in that slot;
#: trusting it unchecked would let a corrupted or forged digest stand in for a
#: real hash for as long as the (size, mtime_ns) pair happens to keep matching.
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass
class ScanResult:
    """One pass's own account of itself.

    `complete` is False the moment anything below could not be trusted: a
    missing or unresolvable scope root, an empty resolved root set, a
    symlinked directory or path component, an `os.walk` error, a `stat()` or a
    read that raised, or a failed cache write. Callers gate on `complete`,
    never on whether `errors` is empty (see the module docstring).

    `files` holds only what this pass actually confirmed the current SHA-256
    of, and `file_ctimes` the `st_ctime_ns` recorded for each of those same
    paths (used only for the relocation ctime check below). `skipped` holds a
    path that changed while being hashed, reported as neither
    present-with-a-known-hash nor absent, and `skipped_shas` keeps the
    (possibly stale) digest computed for it anyway, used only to keep a
    relocation decision from treating that digest as unambiguous.
    `duplicates` holds every SHA-256 this pass saw at two or more paths at
    once amongst `files`, keyed by the hash, values sorted for a stable
    report. `cache_paths_at_start` is the set of paths the cache already knew
    about BEFORE this pass ran, and `cache_written_at` the `written_at` that
    cache carried; both are `None` when the cache was missing, unreadable, or
    in an old format, and so cannot tell a new path from a long-standing one
    at all. `roots` is the concrete, resolved scope roots this pass actually
    used, carried so `availability_events` can ask `doc_scope.in_scope` the
    same question this pass answered rather than whatever `roots=None` might
    resolve to later. `out_of_scope` is REPLACED by `availability_events` on
    every call, not by `scan` itself: the artifact_ids of members whose
    recorded path fell outside `roots` and so were given no availability
    event at all.

    `archived` (2026-10-12, HOS-178) holds the hashes of the confirmed members
    `scan(members=...)` was asked about that sit in an archive folder below a
    scope root, and `archived_scope` every such path, present or not. They are
    kept OUT of `files` on purpose: `files` is what the detector proposes from,
    what duplicates and relocations are judged on, and nothing is ever proposed
    from an archive. `sha_of()` is the one read that sees both.

    `archive_candidates` (2026-10-12, the Part C move) holds the hash of every
    OTHER archive-shaped file below a scope root, its ctime in `file_ctimes`.
    Read only as a relocation candidate: a confirmed member carried into an
    archive folder by a move is found there instead of going `missing`. Never
    in `files`, so the detector still proposes nothing from an archive.

    `gone_out_of_scope` (2026-10-14, HOS-178) holds every confirmed member
    path `scan(members=...)` was asked about that lies outside this pass's
    scope and that an `lstat` found absent. Only such a member may be followed
    to a new in-scope path ("A confirmed member whose old path left the
    scope" in the module docstring); a present one, or one whose `lstat`
    raised anything else, is never in it.
    """
    complete: bool = True
    errors: list[str] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)
    file_ctimes: dict[str, int] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    skipped_shas: dict[str, str] = field(default_factory=dict)
    duplicates: dict[str, list[str]] = field(default_factory=dict)
    cache_paths_at_start: frozenset[str] | None = None
    cache_written_at: int | None = None
    roots: tuple[str, ...] = ()
    out_of_scope: list[str] = field(default_factory=list)
    archived: dict[str, str] = field(default_factory=dict)
    archived_scope: frozenset[str] = frozenset()
    archive_candidates: dict[str, str] = field(default_factory=dict)
    gone_out_of_scope: frozenset[str] = frozenset()

    def sha_of(self, rel: str) -> str | None:
        """The SHA-256 this pass confirmed for `rel`, in scope or an archived member."""
        return self.files.get(rel) or self.archived.get(rel) or self.archive_candidates.get(rel)


def confirmed_member_paths(state: vr.State) -> set[str]:
    """Every path a member of a confirmed family holds; `scan(members=...)` takes it."""
    return {m.path for fam in state.families.values() if fam.confirmed for m in fam.members.values()}


def default_cache_path() -> Path:
    """Where the scan's own cache lives: `runtime:hindsight-sync/cache`.

    A cache is rebuildable, so it lives in the cache directory beside
    hindsight-sync's state rather than in the state directory that holds the
    ledger. Resolved by logical name, never built against the checkout;
    `data_path` answers the directory and this appends the one file this
    module owns inside it.
    """
    return data_path("runtime:hindsight-sync/cache") / "doc-scan-cache.json"


def _sha256_file(path: Path) -> str:
    """The file's SHA-256, read in fixed-size chunks. Never a whole-file read."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_cached_sha(value: object) -> bool:
    """Whether a cache entry's third field is trustworthy enough to reuse."""
    return isinstance(value, str) and bool(_SHA256_RE.fullmatch(value))


def _load_cache(path: Path) -> tuple[dict[str, list], int | None, bool]:
    """The cache's `files` map, its `written_at`, and whether it is usable.

    Usable is True exactly when the cache FILE exists and parses as the
    current `{"written_at": int, "files": {...}}` shape. An old-format cache
    (the flat `{rel: [...]}` shape this module used before `written_at`
    existed), anything else unparseable, or a missing file are all UNUSABLE,
    never treated as "usable but empty": an empty-but-current-shape cache
    still tells the relocation check "nothing was known before this pass, and
    it was written at this exact moment", which an old-format or absent file
    cannot say at all. Any way of being unusable falls back to `({}, None,
    False)`, logged once, never raised; a corrupt cache is only a cache, and
    the next successful write repairs it.
    """
    if not path.exists():
        return {}, None, False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"doc_scan: cache at {path} is unreadable or corrupt, treating as "
              f"unusable: {exc}", file=sys.stderr)
        return {}, None, False
    if (not isinstance(data, dict) or "written_at" not in data or "files" not in data
            or not isinstance(data["written_at"], int) or not isinstance(data["files"], dict)):
        print(f"doc_scan: cache at {path} is not the current "
              f"{{written_at, files}} shape, treating as unusable", file=sys.stderr)
        return {}, None, False
    return data["files"], data["written_at"], True


def _write_cache(path: Path, files: dict[str, list], written_at: int) -> None:
    """Persist the cache: a `.tmp.<pid>` file, then `os.replace`. Never a torn write,
    and two processes writing at once never share a temporary file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    payload = {"written_at": written_at, "files": files}
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _relative_posix(real: Path, root: Path) -> str | None:
    try:
        return PurePosixPath(real.relative_to(root)).as_posix()
    except ValueError:
        return None


def _symlinked_component(root: Path, scope_root: str) -> Path | None:
    """The first symlinked path component between `root` and `scope_root`, if any.

    Checked component by component from `root` down (`root/a`, `root/a/b`,
    ...), so a symlinked PARENT directory of an explicit scope root is caught
    exactly like the scope root itself being one: `scope_root`'s own final
    component is the last one checked, so this single walk covers both.
    """
    current = root
    for part in PurePosixPath(scope_root).parts:
        current = current / part
        if current.is_symlink():
            return current
    return None


def scan(root: Path, cache: Path, *, roots: Iterable[str] | None = None,
         write_cache: bool = True, members: Iterable[str] = ()) -> ScanResult:
    """Walk the scope roots under `root`, hashing what changed since `cache`.

    `roots` is the same `roots` argument `doc_scope.in_scope` takes: omitted,
    the real default scope roots; a self-test passes an invented tuple and
    touches only a temp tree. Each scope root is walked from its OWN starting
    point (never the whole of `root`), so a scope root that does not exist is
    caught as a missing directory rather than silently contributing zero
    files. `write_cache=False` (a dry run) computes and returns the
    same result without ever touching `cache` on disk.

    Any `OSError` this pass meets, whether `os.walk`'s own `onerror`, a
    `stat()`, a read while hashing, or a failed cache write, is recorded in
    `errors` and sets `complete` False; it is never swallowed and never raised
    out of this function. So is any exception raised while resolving the
    scope roots themselves (`doc_scope.DataRootError` / `DataPathError` from
    `doc_scope` refusing to degrade silently, or a bare `OSError` /
    `RuntimeError` from anything else that can go wrong there) and an empty
    resolved root set, which would otherwise read as "nothing to track"
    rather than "something is unresolved". A symlinked directory, or any
    symlinked PARENT component of a scope root, anywhere under `root` is
    refused the same way: this workspace forbids symlinks, and a walk that
    quietly does not descend into one (the default, and always the right
    thing for `os.walk` to do) must not then report the pass that missed
    whatever sits behind it as complete. A file whose (size, mtime_ns) differ
    between the stat taken before hashing and the one taken after is a write
    landing mid-walk: it is recorded in `skipped` (and its stale digest kept
    separately in `skipped_shas`, and its path preserved in the cache with a
    `null` hash so it is not mistaken for new next pass), left out of `files`,
    and does NOT by itself mark the pass incomplete, per the ruling that a
    skip is ordinary and an error is not.

    `members` (normally `confirmed_member_paths(state)`) names the paths of
    confirmed family members. Those that sit in an archive folder below a
    scope root (`doc_scope.in_scope(..., archive_ok=True)` but not plain
    `in_scope`) are hashed in the same walk, under the same rules, into
    `archived` rather than `files`, and listed in `archived_scope`.
    """
    result = ScanResult()
    old_cache, old_written_at, cache_usable = _load_cache(cache)
    result.cache_paths_at_start = frozenset(old_cache) if cache_usable else None
    result.cache_written_at = old_written_at if cache_usable else None
    new_cache: dict[str, list] = {}
    sha_to_paths: dict[str, list[str]] = {}

    try:
        scope_roots = doc_scope._resolve_roots(roots)
    except (DataRootError, doc_scope.DataPathError, OSError, RuntimeError) as exc:
        result.complete = False
        result.errors.append(f"could not resolve scope roots: {exc}")
        scope_roots = ()
    else:
        if not scope_roots:
            result.complete = False
            result.errors.append("no scope roots resolved")
    result.roots = scope_roots
    members = tuple(members)
    archived_scope = frozenset(
        p for p in members
        if not doc_scope.in_scope(p, roots=scope_roots)
        and doc_scope.in_scope(p, roots=scope_roots, archive_ok=True))
    result.archived_scope = archived_scope
    gone: set[str] = set()
    for p in members:
        if doc_scope.in_scope(p, roots=scope_roots) or p in archived_scope:
            continue
        posix = doc_scope._safe_rel(p)
        if posix is None:
            continue
        try:
            os.lstat(root / posix)
        except (FileNotFoundError, NotADirectoryError):
            gone.add(p)
        except OSError as exc:
            result.complete = False
            result.errors.append(f"stat failed for out-of-scope member {p}: {exc}")
    result.gone_out_of_scope = frozenset(gone)

    for scope_root in scope_roots:
        root_dir = root / scope_root
        bad_component = _symlinked_component(root, scope_root)
        if bad_component is not None:
            result.complete = False
            result.errors.append(
                f"symlinked path component under a scope root, refused: {bad_component}")
            continue
        if not root_dir.is_dir():
            result.complete = False
            result.errors.append(f"missing scope root: {scope_root!r} ({root_dir})")
            continue

        def _onerror(exc: OSError, _root_dir=root_dir) -> None:
            result.complete = False
            result.errors.append(f"walk error under {_root_dir}: {exc}")

        for dirpath, dirnames, filenames in os.walk(root_dir, onerror=_onerror, followlinks=False):
            for dname in dirnames:
                dpath = Path(dirpath) / dname
                if dpath.is_symlink():
                    result.complete = False
                    result.errors.append(f"symlinked directory under a scope root, refused: {dpath}")

            for name in filenames:
                real = Path(dirpath) / name
                rel = _relative_posix(real, root)
                if rel is None:
                    continue
                archived = rel in archived_scope
                candidate = False
                if not archived and not doc_scope.in_scope(rel, roots=scope_roots):
                    if not doc_scope.in_scope(rel, roots=scope_roots, archive_ok=True):
                        continue
                    candidate = True
                try:
                    before = real.stat()
                except OSError as exc:
                    result.complete = False
                    result.errors.append(f"stat failed for {rel}: {exc}")
                    continue

                cached = old_cache.get(rel)
                if cached is not None and not isinstance(cached, list):
                    print(f"doc_scan: cache entry for {rel} is malformed (expected a "
                          f"list, got {cached!r}), treating as absent and re-hashing",
                          file=sys.stderr)
                    cached = None
                if (isinstance(cached, list) and len(cached) == 3
                        and cached[0] == before.st_size and cached[1] == before.st_mtime_ns
                        and _valid_cached_sha(cached[2])):
                    sha = cached[2]
                else:
                    try:
                        sha = _sha256_file(real)
                    except OSError as exc:
                        result.complete = False
                        result.errors.append(f"read failed for {rel}: {exc}")
                        continue
                    try:
                        after = real.stat()
                    except OSError as exc:
                        result.complete = False
                        result.errors.append(f"stat failed for {rel}: {exc}")
                        continue
                    if after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns:
                        result.skipped.append(rel)
                        result.skipped_shas[rel] = sha
                        new_cache[rel] = [after.st_size, after.st_mtime_ns, None]
                        continue

                new_cache[rel] = [before.st_size, before.st_mtime_ns, sha]
                if archived:
                    result.archived[rel] = sha
                    continue
                if candidate:
                    result.archive_candidates[rel] = sha
                    result.file_ctimes[rel] = before.st_ctime_ns
                    continue
                result.files[rel] = sha
                result.file_ctimes[rel] = before.st_ctime_ns
                sha_to_paths.setdefault(sha, []).append(rel)

    for sha, rels in sha_to_paths.items():
        if len(rels) > 1:
            result.duplicates[sha] = sorted(rels)

    if not result.complete:
        # An incomplete pass persists the UNION of what it already knew and
        # what it saw this time, and never drops a key the previous cache
        # held: see the module docstring's cache section, case (c).
        merged = dict(old_cache)
        merged.update(new_cache)
        new_cache = merged

    if write_cache:
        try:
            _write_cache(cache, new_cache, time.time_ns())
        except OSError as exc:
            # A stale cache on disk is worse than no write at all, per case
            # (d): this pass's own findings are no longer safely reflected in
            # what the NEXT pass will load, so this pass itself is incomplete.
            result.complete = False
            result.errors.append(f"could not write cache at {cache}: {exc}")

    return result


def _ts_ns(ts: str | None) -> int | None:
    """A registry event `ts` (ISO 8601) as epoch nanoseconds, or None if absent or unreadable."""
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except (TypeError, ValueError) as exc:
        print(f"doc_scan: unreadable registry ts {ts!r} ({exc}), late relocation not offered",
              file=sys.stderr)
        return None
    if parsed.tzinfo is None:
        return None
    return int(parsed.timestamp()) * 1_000_000_000 + parsed.microsecond * 1_000


def _all_members(state: vr.State) -> list[vr.Member]:
    """Every member of every family, confirmed or still proposed, one per artifact_id.

    Per the plan: "Members of ALL families (confirmed and proposed) are
    checked." The same artifact_id can legitimately appear more than once
    across this traversal: `doc-versions.py`'s own `record` reuses an existing
    artifact_id (via `artifact_for`) when a path is already known to the
    registry, so the identical artifact can sit in one family's confirmed
    `members` and another, still-unconfirmed family's `proposed` at once, and
    `version_registry.replay`'s invariants only forbid two CONFIRMED families
    from sharing one, never a confirmed and a proposed. `dict.setdefault`
    keeps only the first occurrence found (families and their own `members`
    before `proposed`, both in a deterministic insertion order for a given
    replay), so every artifact_id below is unique and `availability_events`
    never emits two events for the same artifact in one pass.
    """
    seen: dict[str, vr.Member] = {}
    for fam in state.families.values():
        for m in fam.members.values():
            seen.setdefault(m.artifact_id, m)
        for m in fam.proposed.values():
            seen.setdefault(m.artifact_id, m)
    return list(seen.values())


def availability_events(state: vr.State, prev_absent: set[str],
                         result: ScanResult) -> tuple[list[dict], set[str]]:
    """The events this pass justifies, and the updated "currently unavailable" set.

    Emits nothing and returns `prev_absent` unchanged when `result.complete`
    is False: an incomplete pass proves nothing about what is actually gone.
    Otherwise, for every member of every family (confirmed or proposed, one
    event per artifact_id, per `_all_members`):

    - out of scope (`doc_scope.in_scope(member.path, roots=result.roots)` is
      False and the path is not in `result.archived_scope`): no event at all; `result.out_of_scope` is REPLACED (never
      appended to) with the fresh list of such artifact_ids for this call.
      The one exception is a confirmed member whose path is in
      `result.gone_out_of_scope` and whose bytes pass the test in "A
      confirmed member whose old path left the scope": `relocated`, and it
      is not listed out of scope.
    - present (in `result.files` or `result.skipped`): a member whose state
      was `unavailable` or `missing` gets `available`; one already `ok` gets
      nothing.
    - absent, and the five relocation conditions in the module docstring all
      hold for its SHA-256 (4 and 5 replaced by the `seen_at` test for a
      member already absent, see "followed late"): `relocated`, carrying the new path and the
      confirming SHA-256.
    - absent otherwise: a member already in `prev_absent` (state
      `unavailable`) gets `missing`; a member whose state is already
      `missing` gets nothing, staying silently missing; anything else gets
      `unavailable` and joins the returned set.
    """
    if not result.complete:
        return [], prev_absent

    members = _all_members(state)
    confirmed_ids = {m.artifact_id for fam in state.families.values() if fam.confirmed
                     for m in fam.members.values()}
    in_scope_members = []
    gone_members = []
    out_of_scope: list[str] = []
    for m in members:
        if doc_scope.in_scope(m.path, roots=result.roots) or m.path in result.archived_scope:
            in_scope_members.append(m)
        elif m.path in result.gone_out_of_scope and m.artifact_id in confirmed_ids:
            gone_members.append(m)
        else:
            out_of_scope.append(m.artifact_id)

    recorded_paths = {m.path for m in members}
    sha_to_paths: dict[str, list[str]] = {}
    for rel, sha in list(result.files.items()) + list(result.archive_candidates.items()):
        sha_to_paths.setdefault(sha, []).append(rel)
    skipped_shas = set(result.skipped_shas.values())

    events: list[dict] = []
    new_absent = set(prev_absent)
    present_members = []
    absent_members = []
    for m in in_scope_members:
        present = m.path in result.files or m.path in result.archived or m.path in result.skipped
        (present_members if present else absent_members).append(m)

    for m in present_members:
        if m.state in ("unavailable", "missing"):
            events.append({"type": "available", "artifact_id": m.artifact_id})
        new_absent.discard(m.artifact_id)

    absent_by_sha: dict[str, list[vr.Member]] = {}
    for m in absent_members + gone_members:
        absent_by_sha.setdefault(m.sha256, []).append(m)

    # A confirmed member whose old path left the scope: conditions 1-3, and the
    # `seen_at` test in place of 4 and 5 (module docstring). Anything short of
    # that stays out of scope with no event, exactly as before.
    for m in gone_members:
        candidates = sha_to_paths.get(m.sha256, [])
        seen_at_ns = _ts_ns(m.seen_at)
        if (len(absent_by_sha[m.sha256]) == 1
                and len(candidates) == 1
                and candidates[0] not in recorded_paths
                and m.sha256 not in skipped_shas
                and seen_at_ns is not None
                and result.file_ctimes.get(candidates[0], 0) > seen_at_ns):
            events.append({"type": "relocated", "artifact_id": m.artifact_id,
                           "path": candidates[0], "sha256": m.sha256})
            new_absent.discard(m.artifact_id)
        else:
            out_of_scope.append(m.artifact_id)
    result.out_of_scope = out_of_scope

    for m in absent_members:
        aid = m.artifact_id
        candidates = sha_to_paths.get(m.sha256, [])
        known_before = result.cache_paths_at_start
        is_new_path = (len(candidates) == 1 and known_before is not None
                       and candidates[0] not in known_before)
        has_new_ctime = (
            is_new_path and result.cache_written_at is not None
            and result.file_ctimes.get(candidates[0], 0) > result.cache_written_at
        )
        # The late-scope route: see "A member already absent is followed late"
        # in the module docstring. Conditions 1-3 still hold below.
        seen_at_ns = _ts_ns(m.seen_at)
        moved_after_seen = (
            m.state in ("unavailable", "missing") and len(candidates) == 1
            and seen_at_ns is not None
            and result.file_ctimes.get(candidates[0], 0) > seen_at_ns
        )
        unambiguous = (
            len(absent_by_sha[m.sha256]) == 1
            and len(candidates) == 1
            and candidates[0] not in recorded_paths
            and m.sha256 not in skipped_shas
            and ((is_new_path and has_new_ctime) or moved_after_seen)
        )
        if unambiguous:
            events.append({"type": "relocated", "artifact_id": aid,
                            "path": candidates[0], "sha256": m.sha256})
            new_absent.discard(aid)
            continue

        if aid in prev_absent:
            events.append({"type": "missing", "artifact_id": aid})
            new_absent.discard(aid)  # now missing, not merely unavailable
        elif m.state == "missing":
            pass  # already missing; stays silently missing until it reappears
        else:
            events.append({"type": "unavailable", "artifact_id": aid})
            new_absent.add(aid)

    return events, new_absent
