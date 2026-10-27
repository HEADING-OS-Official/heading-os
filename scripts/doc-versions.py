#!/usr/bin/env python3
"""doc-versions.py, the document version registry's own CLI.

    doc-versions.py self-test                                  # no network, temp fixtures only
    doc-versions.py record <from> <to>                          # HELM proposes a new family
    doc-versions.py pending [--count]                           # families awaiting a human decision
    doc-versions.py show <family>                                # numbered members, diffs, origin
    doc-versions.py confirm <family>                             # proposed members become members
    doc-versions.py decide <family> <n> --expect-sha SHA        # n and SHA exactly as `show` printed them
    doc-versions.py reject <family>|<path> <path>...             # "these are not versions"
    doc-versions.py remove-member <family> <n>                  # a mistaken inclusion
    doc-versions.py merge <into> <from>                          # one revision line proposed as two families
    doc-versions.py dismiss <family> [--reason TEXT]             # a proposal left with fewer than two members
    doc-versions.py summary <family> <from_n> <to_n> <text>     # lazily-written change description
    doc-versions.py restore <path>                               # queue <path> for the next hindsight-sync push
    doc-versions.py scan [--dry-run]                             # detector: propose families, never decide

`--root` and `--registry` exist on every subcommand so the self-test can point
at a temp tree; their real defaults are `get_data_root()` and
`vr.registry_path()`, resolved lazily (never at argparse construction, so
`--help` works on a bare engine clone with no data repository).

Every write goes through `scripts.utils.version_registry.append()`, which
locks, replays and refuses an invariant-breaking event before writing
anything. `decide` on a proposed member appends `member_confirmed` then
`decided` under one outer `locked()`, because choosing a still-proposed
member is itself the act of confirming it.

Every path an event carries, and every `--root`-relative argument this CLI
takes, is POSIX-relative to the root. `_resolve_under_root()` is the one place
that turns such a path into a real file, and it refuses both an escape from
the root and a path that does not exist.

`_diff_text()` is the one seam `show`'s diff goes through, and delegates to
`scripts.utils.doc_scope.text_for()`: a document's own text for `.md`, a valid
`-extract.md` twin's text for every other tracked format, `None`
("comparison unavailable") otherwise.

`restore` writes one line into the `hindsight-sync.py` ledger, in the same
shape (`path`, `requeue`, `reason`, `sent`) that `status --requeue-failed`
prints for its caller to append. `hindsight-sync.py` CAN be loaded at runtime:
`_hs()` below, in the self-test section, already does it with
`importlib.util.spec_from_file_location`, because the module's own name is
hyphenated and a plain `from scripts.hindsight-sync import ...` is a syntax
error. That loader is deliberately kept to the self-tests: calling it from
`cmd_restore` would pull the whole sibling script, with its own argparse,
`urllib` and server-settings surface, into one CLI command's hot path just to
read a single path. The shape itself has no standalone function to import
either way: it is built inline inside `cmd_status`. So `_default_ledger()`
and `cmd_restore()` below reconstruct the ledger address and the four keys
directly, and `_st_restore_ledger_matches_hindsight_sync` pins
`_default_ledger()` against `_hs()._ledger()` so the two cannot drift apart
unnoticed.
"""
from __future__ import annotations

import argparse
import contextlib
import difflib
import hashlib
import io
import json
import multiprocessing
import os
import random
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils import doc_detector  # noqa: E402
from scripts.utils import doc_scan  # noqa: E402
from scripts.utils import doc_scope  # noqa: E402
from scripts.utils import memory_plan  # noqa: E402
from scripts.utils import version_registry as vr  # noqa: E402
from scripts.utils.data_path import DataPathError, data_path, data_write_dir  # noqa: E402
from scripts.utils.paths import get_data_root  # noqa: E402

# ============================================================
# Public API (Task 2 interfaces)
# ============================================================


def artifact_for(state: vr.State, path: str) -> str | None:
    """The `artifact_id` already known at `path`, across every family, or `None`.

    Searches both confirmed members and still-proposed ones, because a path
    reused by an undecided proposal is exactly the case that must not mint a
    second `artifact_id` for what is really the same document.
    """
    for fam in state.families.values():
        for m in list(fam.members.values()) + list(fam.proposed.values()):
            if m.path == path:
                return m.artifact_id
    return None


def _confirmed_owner(state: vr.State, path: str) -> str | None:
    """The `family_id` that already holds `path` as a CONFIRMED member, or `None`.

    Never looks at `proposed`: a still-proposed member does not yet own its
    path in the sense that matters here, which is the one-confirmed-family-
    per-artifact invariant `replay()` enforces at `family_confirmed` time. A
    second `record` naming a path already confirmed elsewhere would otherwise
    mint a dead-end proposed family that can never itself be confirmed.
    """
    for fam in state.families.values():
        if not fam.confirmed:
            continue
        for m in fam.members.values():
            if m.path == path:
                return fam.family_id
    return None


def numbered(fam: vr.Family) -> list[vr.Member]:
    """Members then proposed, each block sorted by path.

    Stable on purpose: as long as nothing else in the family changes between
    a `show` and the `decide` that follows it, the number `show` printed still
    names the same artifact.
    """
    return (sorted(fam.members.values(), key=lambda m: m.path)
            + sorted(fam.proposed.values(), key=lambda m: m.path))


# ============================================================
# Small helpers
# ============================================================


def _resolve_root(args: argparse.Namespace) -> Path:
    return Path(args.root) if args.root else get_data_root()


def _resolve_registry(args: argparse.Namespace) -> Path:
    return Path(args.registry) if args.registry else vr.registry_path()


def _default_ledger() -> Path:
    """The `hindsight-sync.py` ledger, resolved the same way that script's own
    `_ledger()` resolves it. See the module docstring for why this is a small
    duplicate of that call rather than an import."""
    return data_path("runtime:hindsight-sync/state") / "ledger.jsonl"


def _resolve_under_root(root: Path, rel: str) -> Path:
    """The real file `rel` (POSIX, root-relative) names, or a clear refusal.

    Refuses a path that resolves outside `root` and a path that is not
    currently a file there. Both are the caller's mistake, never a crash.
    """
    root_r = root.resolve()
    candidate = (root_r / rel).resolve()
    if candidate != root_r and root_r not in candidate.parents:
        raise ValueError(f"{rel!r} resolves outside root {root_r}")
    if not candidate.is_file():
        raise ValueError(f"{rel!r} does not exist under {root_r}")
    return candidate


def _sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _diff_text(root: Path, rel: str) -> str | None:
    """Text usable for a diff, or `None` ("comparison unavailable").

    Delegates to `doc_scope.text_for`: a document's own text for `.md`, a
    valid `-extract.md` twin's text for every other tracked format, `None`
    otherwise.
    """
    return doc_scope.text_for(root, rel)


def _origin(events: list[dict], family_id: str) -> str:
    """The `source` the family's own `family_proposed` event carried."""
    for ev in events:
        if ev["type"] == "family_proposed" and ev.get("family_id") == family_id:
            return ev.get("source", "unknown")
    return "unknown"


def _member_by_number(fam: vr.Family, number: str) -> vr.Member | None:
    members = numbered(fam)
    try:
        idx = int(number)
    except ValueError:
        return None
    if idx < 1 or idx > len(members):
        return None
    return members[idx - 1]


# ============================================================
# Subcommands
# ============================================================


def cmd_record(args: argparse.Namespace) -> int:
    root = _resolve_root(args)
    reg = _resolve_registry(args)
    try:
        from_real = _resolve_under_root(root, args.from_path)
        to_real = _resolve_under_root(root, args.to_path)
    except ValueError as exc:
        print(f"doc-versions: {exc}", file=sys.stderr)
        return 2
    family_id = uuid.uuid4().hex
    root_r = root.resolve()
    with vr.locked(reg):
        st = vr.replay(vr.read_events(reg)[0])
        members = []
        for real in (from_real, to_real):
            # the real file's own root-relative path, never the form typed on the command line
            rel_posix = real.relative_to(root_r).as_posix()
            owner = _confirmed_owner(st, rel_posix)
            if owner is not None:
                print(f"doc-versions: {rel_posix!r} is already a confirmed member of family "
                      f"{owner!r}", file=sys.stderr)
                return 2
            aid = artifact_for(st, rel_posix) or uuid.uuid4().hex
            stat = real.stat()
            members.append({
                "artifact_id": aid,
                "path": rel_posix,
                "sha256": _sha256_of(real),
                "size": stat.st_size,
                "mtime": stat.st_mtime,
            })
        try:
            vr.append({"type": "family_proposed", "family_id": family_id,
                       "source": "created", "members": members}, path=reg)
        except vr.RegistryCorrupt as exc:
            print(f"doc-versions: refused: {exc}", file=sys.stderr)
            return 2
    print(f"proposed family {family_id}: {args.from_path} -> {args.to_path}")
    return 0


def cmd_pending(args: argparse.Namespace) -> int:
    reg = _resolve_registry(args)
    st = vr.replay(vr.read_events(reg)[0])
    # A rejected family has nothing left to decide: its rejection governs it (2026-10-12).
    fams = [f for f in st.families.values()
            if (not f.confirmed or f.actual is None or f.proposed) and not vr.is_rejected(f, st)]
    if args.count:
        print(len(fams))
        return 0
    if not fams:
        print("nothing pending")
        return 0
    for fam in sorted(fams, key=lambda f: f.family_id):
        if not fam.confirmed:
            status = "unconfirmed"
        elif fam.proposed:
            status = "has proposed member(s)"
        else:
            status = "awaiting a decision"
        print(f"{fam.family_id}  {status}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    root = _resolve_root(args)
    reg = _resolve_registry(args)
    events, _ = vr.read_events(reg)
    st = vr.replay(events)
    fam = st.families.get(args.family)
    if fam is None:
        print(f"doc-versions: no such family {args.family!r}", file=sys.stderr)
        return 2
    origin = _origin(events, args.family)
    members = numbered(fam)
    actual = fam.members.get(fam.actual) if fam.actual else None
    actual_text = _diff_text(root, actual.path) if actual is not None else None
    print(f"family {fam.family_id} ({'confirmed' if fam.confirmed else 'proposed'}, origin {origin})")
    for i, m in enumerate(members, start=1):
        real = root / m.path
        try:
            mtime = datetime.fromtimestamp(real.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
            current = _sha256_of(real)
        except OSError as exc:
            print(f"doc-versions: cannot read {m.path}: {exc}", file=sys.stderr)
            mtime = current = "unavailable"
        kind = "member" if m.artifact_id in fam.members else "proposed"
        line = f"  {i}. {m.path}  file time (technical): {mtime}  {kind}  state {m.state}"
        if actual is not None and m.artifact_id == actual.artifact_id:
            line += "  (actual)"
            if current != "unavailable" and current != fam.actual_sha:
                line += "  changed after decision"
        print(line)
        print(f"     sha256 now: {current}")
        if fam.confirmed and current != "unavailable":
            print(f"     to choose it: doc-versions.py decide {fam.family_id} {i} --expect-sha {current}")
        if actual is not None and m.artifact_id != actual.artifact_id:
            this_text = _diff_text(root, m.path)
            if this_text is None or actual_text is None:
                print("     comparison unavailable")
            else:
                diff_lines = list(difflib.unified_diff(
                    actual_text.splitlines(keepends=True), this_text.splitlines(keepends=True),
                    fromfile=actual.path, tofile=m.path))[:40]
                for dl in diff_lines:
                    print("     " + dl.rstrip("\n"))
            summary_text = fam.summaries.get((fam.actual_sha, m.sha256)) or fam.summaries.get((m.sha256, fam.actual_sha))
            if summary_text:
                print(f"     summary: {summary_text}")
    return 0


def cmd_confirm(args: argparse.Namespace) -> int:
    reg = _resolve_registry(args)
    try:
        vr.append({"type": "family_confirmed", "family_id": args.family}, path=reg)
    except vr.RegistryCorrupt as exc:
        print(f"doc-versions: refused: {exc}", file=sys.stderr)
        return 2
    print(f"confirmed family {args.family}")
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    """Every read this decides on, and the writes it makes, share one lock.

    Replay, member resolution, the SHA-256 recompute and the `--expect-sha`
    comparison all happen INSIDE `vr.locked(reg)`, not before it: reading state
    outside the lock and only locking the append leaves a window where another
    writer's `decide` or `record` lands between the read and the write, so the
    `decided` event this call appends could carry a SHA-256 or a family state
    that is already stale by the time it is written. Taking the lock first
    closes that window; the `decided` event's `sha256` is the one computed
    inside this same locked block.
    """
    if not args.expect_sha:
        print("doc-versions: decide needs --expect-sha <sha>, exactly as `show` printed it for that member",
              file=sys.stderr)
        return 2
    root = _resolve_root(args)
    reg = _resolve_registry(args)
    with vr.locked(reg):
        st = vr.replay(vr.read_events(reg)[0])
        fam = st.families.get(args.family)
        if fam is None:
            print(f"doc-versions: no such family {args.family!r}", file=sys.stderr)
            return 2
        if not fam.confirmed:
            print("confirm the family first", file=sys.stderr)
            return 2
        member = _member_by_number(fam, args.number)
        if member is None:
            print(f"doc-versions: no member numbered {args.number!r}", file=sys.stderr)
            return 2
        try:
            real = _resolve_under_root(root, member.path)
        except ValueError as exc:
            print(f"doc-versions: {exc}", file=sys.stderr)
            return 2
        current_sha = _sha256_of(real)
        if args.expect_sha != current_sha:
            print("changed since show; run show again", file=sys.stderr)
            return 2
        is_proposed = member.artifact_id in fam.proposed
        try:
            if is_proposed:
                vr.append({"type": "member_confirmed", "family_id": args.family,
                           "artifact_id": member.artifact_id}, path=reg)
            vr.append({"type": "decided", "family_id": args.family,
                       "artifact_id": member.artifact_id, "sha256": current_sha}, path=reg)
        except vr.RegistryCorrupt as exc:
            print(f"doc-versions: refused: {exc}", file=sys.stderr)
            return 2
    print(f"decided: family {args.family} -> {member.path}")
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    """Record "these are not versions of each other", keyed by content.

    A CONFIRMED family is refused: its membership is a decision already taken,
    and rejecting its revisions would let the detector propose its paths into a
    second family. A mistaken member leaves through `remove-member` instead.
    State is read and the event appended under one lock.
    """
    root = _resolve_root(args)
    reg = _resolve_registry(args)
    targets = args.targets
    with vr.locked(reg):
        st = vr.replay(vr.read_events(reg)[0])
        if len(targets) == 1 and targets[0] in st.families:
            fam = st.families[targets[0]]
            if fam.confirmed:
                print("doc-versions: that family is confirmed; take a mistaken member out with "
                      f"`doc-versions.py remove-member {fam.family_id} <n>` instead", file=sys.stderr)
                return 2
            shas = sorted({m.sha256 for m in list(fam.members.values()) + list(fam.proposed.values())})
            if len(shas) < 2:
                print("doc-versions: that family has fewer than two revisions to reject", file=sys.stderr)
                return 2
        else:
            if len(targets) < 2:
                print("doc-versions: reject needs a family id, or at least two paths", file=sys.stderr)
                return 2
            try:
                reals = [_resolve_under_root(root, t) for t in targets]
            except ValueError as exc:
                print(f"doc-versions: {exc}", file=sys.stderr)
                return 2
            root_r = root.resolve()
            for real in reals:
                owner = _confirmed_owner(st, real.relative_to(root_r).as_posix())
                if owner is not None:
                    print("doc-versions: that family is confirmed; take a mistaken member out with "
                          f"`doc-versions.py remove-member {owner} <n>` instead", file=sys.stderr)
                    return 2
            shas = [_sha256_of(real) for real in reals]
        try:
            vr.append({"type": "rejected", "shas": shas}, path=reg)
        except vr.RegistryCorrupt as exc:
            print(f"doc-versions: refused: {exc}", file=sys.stderr)
            return 2
    print(f"rejected {len(shas)} revision(s)")
    return 0


def cmd_remove_member(args: argparse.Namespace) -> int:
    """Take member `n` out of a family; the number is resolved under the same lock as the write."""
    reg = _resolve_registry(args)
    with vr.locked(reg):
        st = vr.replay(vr.read_events(reg)[0])
        fam = st.families.get(args.family)
        if fam is None:
            print(f"doc-versions: no such family {args.family!r}", file=sys.stderr)
            return 2
        member = _member_by_number(fam, args.number)
        if member is None:
            print(f"doc-versions: no member numbered {args.number!r}", file=sys.stderr)
            return 2
        try:
            vr.append({"type": "member_removed", "family_id": args.family,
                       "artifact_id": member.artifact_id}, path=reg)
        except vr.RegistryCorrupt as exc:
            print(f"doc-versions: refused: {exc}", file=sys.stderr)
            return 2
    print(f"removed {member.path} from family {args.family}")
    return 0


def _all_of(fam: vr.Family) -> list[vr.Member]:
    return list(fam.members.values()) + list(fam.proposed.values())


def _append_all(reg: Path, events: list[dict]) -> None:
    """Append `events` as one batch: replay them all on a trial first, so a refusal writes none.

    The caller holds `vr.locked(reg)`, so nothing lands between the trial and the writes.
    """
    existing = vr.read_events(reg)[0]
    next_seq = (existing[-1]["seq"] + 1) if existing else 1
    vr.replay(existing + [{"schema_version": vr.SCHEMA_VERSION, "event_id": f"trial-{i}",
                           "seq": next_seq + i, "ts": "", "actor": "helm", **ev}
                          for i, ev in enumerate(events)])
    for ev in events:
        vr.append(ev, path=reg)


def cmd_merge(args: argparse.Namespace) -> int:
    """Fold family `from` into family `into`, then dismiss the emptied `from`.

    For the detector proposing one revision line as two families. Every member
    of `from` joins `into` as a proposed member (a confirmed `into` then needs
    `confirm` or `decide` again, exactly as for a member the detector adds), and
    `from` is dismissed with the reason "merged into <into>". `from` must be
    unconfirmed: a confirmed family's membership is a decision already taken.
    Nothing is said about content, so no rejection is recorded.
    """
    reg = _resolve_registry(args)
    with vr.locked(reg):
        st = vr.replay(vr.read_events(reg)[0])
        into, src = st.families.get(args.into), st.families.get(args.source)
        for fid, fam in ((args.into, into), (args.source, src)):
            if fam is None:
                print(f"doc-versions: no such family {fid!r}", file=sys.stderr)
                return 2
        if args.into == args.source:
            print("doc-versions: a family cannot be merged into itself", file=sys.stderr)
            return 2
        if src.confirmed:
            print(f"doc-versions: family {args.source} is confirmed; take members out with remove-member "
                  "rather than merging it away", file=sys.stderr)
            return 2
        known = {m.artifact_id for m in _all_of(into)}
        events = [{"type": "member_proposed", "family_id": args.into, "source": "merge",
                   "member": {"artifact_id": m.artifact_id, "path": m.path, "sha256": m.sha256}}
                  for m in sorted(_all_of(src), key=lambda m: m.path) if m.artifact_id not in known]
        events.append({"type": "family_dismissed", "family_id": args.source,
                       "reason": f"merged into {args.into}"})
        try:
            _append_all(reg, events)
        except vr.RegistryCorrupt as exc:
            print(f"doc-versions: refused, nothing written: {exc}", file=sys.stderr)
            return 2
    print(f"merged family {args.source} into {args.into}: {len(events) - 1} member(s) proposed")
    return 0


def cmd_dismiss(args: argparse.Namespace) -> int:
    """Take an unconfirmed family with fewer than two members out of play.

    Says nothing about content (that is `reject`): the family is simply no
    longer a question, because one file is not a version line. A family with
    two or more revisions is refused and pointed at `reject` or `merge`.
    """
    reg = _resolve_registry(args)
    with vr.locked(reg):
        st = vr.replay(vr.read_events(reg)[0])
        fam = st.families.get(args.family)
        if fam is None:
            print(f"doc-versions: no such family {args.family!r}", file=sys.stderr)
            return 2
        if fam.confirmed:
            print("doc-versions: that family is confirmed; it cannot be dismissed", file=sys.stderr)
            return 2
        if len(_all_of(fam)) >= 2:
            print("doc-versions: that family has two or more members; use `reject` (not versions) or "
                  "`merge` (one line proposed twice) instead", file=sys.stderr)
            return 2
        try:
            vr.append({"type": "family_dismissed", "family_id": args.family,
                       "reason": args.reason or f"{len(_all_of(fam))} member(s), not a version line"}, path=reg)
        except vr.RegistryCorrupt as exc:
            print(f"doc-versions: refused: {exc}", file=sys.stderr)
            return 2
    print(f"dismissed family {args.family}")
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    """Store a change description for two members; numbers and hashes are resolved under the lock."""
    root = _resolve_root(args)
    reg = _resolve_registry(args)
    with vr.locked(reg):
        st = vr.replay(vr.read_events(reg)[0])
        fam = st.families.get(args.family)
        if fam is None:
            print(f"doc-versions: no such family {args.family!r}", file=sys.stderr)
            return 2
        from_m = _member_by_number(fam, args.from_n)
        to_m = _member_by_number(fam, args.to_n)
        if from_m is None or to_m is None:
            print("doc-versions: bad member number", file=sys.stderr)
            return 2
        try:
            from_sha = _sha256_of(_resolve_under_root(root, from_m.path))
            to_sha = _sha256_of(_resolve_under_root(root, to_m.path))
        except ValueError as exc:
            print(f"doc-versions: {exc}", file=sys.stderr)
            return 2
        try:
            vr.append({"type": "summary", "family_id": args.family,
                       "from_sha": from_sha, "to_sha": to_sha, "text": args.text}, path=reg)
        except vr.RegistryCorrupt as exc:
            print(f"doc-versions: refused: {exc}", file=sys.stderr)
            return 2
    print(f"summary stored for family {args.family}: {from_sha[:12]}..{to_sha[:12]}")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    """Queue `path` for the next `hindsight-sync.py push` to send it again.

    Writes exactly one line to the ledger, in the shape
    `status --requeue-failed` prints (see the module docstring). `path` must
    be root-relative POSIX and must name a real file under `--root`:
    `_resolve_under_root` refuses an absolute path (an absolute right-hand
    operand of `Path.__truediv__` discards `root`, so the joined candidate
    resolves outside it), a `..` escape (the resolved candidate lands outside
    `root`'s parents), and a path naming nothing on disk, in every case before
    anything is written. The append itself is one `open("a")` call writing one
    short line, which is atomic on a POSIX filesystem for a write well under
    the kernel's atomic-write limit, the same footing `version_registry.append`
    stands on for its own single-line writes.
    """
    root = _resolve_root(args)
    try:
        real = _resolve_under_root(root, args.path)
    except ValueError as exc:
        print(f"doc-versions: {exc}", file=sys.stderr)
        return 2
    try:
        state = vr.replay(vr.read_events(_resolve_registry(args))[0])
    except (vr.RegistryCorrupt, DataPathError) as exc:
        print(f"doc-versions: the version registry cannot be read ({exc}); restore refused until it can",
              file=sys.stderr)
        return 2
    rel_posix = real.relative_to(root.resolve()).as_posix()
    role = _held_role(state, rel_posix)
    if role:
        print(f"doc-versions: {args.path} is {role}; push holds it and would ignore the requeue, so "
              "nothing was queued (decide or remove-member first)", file=sys.stderr)
        return 2
    try:
        ledger = Path(args.ledger) if args.ledger else _default_ledger()
    except DataPathError as exc:
        print(f"doc-versions: ledger address unresolved: {exc}", file=sys.stderr)
        return 2
    line = {"path": rel_posix, "requeue": True,
            "reason": "manual restore via doc-versions.py restore",
            "sent": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    print(f"queued {rel_posix} for re-send on the next push")
    return 0


def _held_role(state: vr.State, rel: str) -> str | None:
    """Why `hindsight-sync.py push` would hold `rel` back, or `None` when it sends it.

    Mirrors the roles `memory_plan.plan()` gives members of a confirmed family
    with a decided actual: a history member (not the actual) or a proposed one.
    `rel` may be the original or its `-extract.md` twin.
    """
    for fam in state.families.values():
        if not fam.confirmed or fam.actual is None:
            continue
        for kind, group in (("history of family", fam.members), ("a proposed member of family", fam.proposed)):
            for aid, m in group.items():
                if group is fam.members and aid == fam.actual:
                    continue
                if rel == m.path or rel == memory_plan._twin_rel(m.path):
                    return f"{kind} {fam.family_id}"
    return None


def _print_detection(result: doc_scan.ScanResult, det) -> None:
    fams = [e for e in det.events if e["type"] == "family_proposed"]
    mems = [e for e in det.events if e["type"] == "member_proposed"]
    print(f"files scanned: {len(result.files)} (complete: {'yes' if result.complete else 'no'})")
    for err in result.errors:
        print(f"error: {err}")
    print(f"skipped, changed during the walk: {len(result.skipped)}")
    print(f"texts: {det.with_text} with text, {det.without_text} without")
    print(f"proposed families: {len(fams)}")
    print(f"member proposals: {len(mems)}")
    print(f"duplicates: {len(det.duplicates)} group(s)")
    for group in det.duplicates:
        print(f"  duplicate: {' | '.join(group)}")
    print(f"representations: {len(det.representations)} group(s)")
    for group in det.representations:
        print(f"  representation: {' | '.join(group)}")
    for note in det.notes:
        print(f"note: {note}")
    for i, ev in enumerate(fams + mems, 1):
        if ev["type"] == "family_proposed":
            print(f"proposal {i}: new family, {len(ev['members'])} files")
            members = ev["members"]
        else:
            print(f"proposal {i}: new member for family {ev['family_id']}")
            members = [ev["member"]]
        for m in members:
            print(f"  member: {m['path']}")
        for r in ev["reasons"]:
            print(f"  reason: {r}")
    print(f"near misses: {len(det.near_misses)} pair(s) scored "
          f"{doc_detector.NEAR_MISS_FLOOR} or more but not linked (under their threshold or refused by a name rule)")
    for i, (score, a, b, reason) in enumerate(det.near_misses, 1):
        print(f"near miss {i}: {score:.2f}")
        print(f"  member: {a}")
        print(f"  member: {b}")
        print(f"  reason: {reason}")


def cmd_scan(args: argparse.Namespace) -> int:
    """Scan, detect, print; append the proposals and persist both caches unless `--dry-run`."""
    root = _resolve_root(args)
    reg = _resolve_registry(args)
    scan_cache = Path(args.scan_cache) if args.scan_cache else doc_scan.default_cache_path()
    det_cache = Path(args.detector_cache) if args.detector_cache else doc_detector.default_cache_path()
    roots = tuple(args.scope_root) if args.scope_root else None

    state = vr.replay(vr.read_events(reg)[0])
    result = doc_scan.scan(root, scan_cache, roots=roots, write_cache=not args.dry_run,
                           members=doc_scan.confirmed_member_paths(state))
    det = doc_detector.detect(state, result, root, cache=doc_detector.load_cache(det_cache))
    _print_detection(result, det)
    if args.dry_run:
        print("dry run: nothing written")
        return 0
    doc_detector.save_cache(det_cache, det.cache)
    with vr.locked(reg):
        try:
            written = doc_detector.append_proposals(reg, state, det, result, root)
        except vr.RegistryCorrupt as exc:
            print(f"doc-versions: refused the whole batch, nothing appended: {exc}", file=sys.stderr)
            return 2
    print(f"appended {len(written)} event(s)")
    return 0


# ============================================================
# Self-test harness (Task 1 fixtures, plus Task 2 scenarios)
# ============================================================


def _m(aid: str) -> dict:
    return {"artifact_id": aid, "path": f"d/{aid}.md", "sha256": hashlib.sha256(aid.encode()).hexdigest()}


_SEQ = iter(range(1, 10_000))


def _ev(kind: str, **kw) -> dict:
    return {"schema_version": 1, "event_id": uuid.uuid4().hex, "seq": next(_SEQ), "ts": "2026-01-01T00:00:00Z",
            "actor": "helm", "type": kind, **kw}


def _tree(tmp: Path, files: dict[str, str]) -> Path:
    """Write `files` (root-relative path -> text) under `tmp/root`; return that root."""
    root = tmp / "root"
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root


def _st_truncated_tail_tolerated(tmp: Path) -> None:
    reg = tmp / "reg.jsonl"
    vr.append({"type": "family_proposed", "family_id": "f1", "source": "created",
               "members": [{"artifact_id": "a1", "path": "d/x-v1.md", "sha256": "1" * 64}]}, path=reg)
    with reg.open("a", encoding="utf-8") as fh:
        fh.write('{"type": "decided", "fam')  # crash mid-append
    events, warnings = vr.read_events(reg)
    assert len(events) == 1 and warnings, warnings


def _st_append_after_truncated_tail_recovers(tmp: Path) -> None:
    """A crash-torn tail is repaired, not glued onto, by the next append."""
    reg = tmp / "reg.jsonl"
    vr.append({"type": "family_proposed", "family_id": "f1", "source": "created",
               "members": [_m("a1")]}, path=reg)
    fragment = '{"type": "decided", "fam'
    with reg.open("a", encoding="utf-8") as fh:
        fh.write(fragment)  # crash mid-append, no trailing newline
    vr.append({"type": "family_proposed", "family_id": "f2", "source": "created",
               "members": [_m("a2")]}, path=reg)
    vr.append({"type": "family_proposed", "family_id": "f3", "source": "created",
               "members": [_m("a3")]}, path=reg)
    events, warnings = vr.read_events(reg)
    assert len(events) == 3 and not warnings, (len(events), warnings)
    torn_path = reg.with_name(reg.name + ".torn")
    assert torn_path.exists(), "the torn fragment must be preserved in a sidecar"
    assert torn_path.read_text(encoding="utf-8") == fragment, torn_path.read_text(encoding="utf-8")


def _st_append_after_unterminated_valid_line_keeps_it(tmp: Path) -> None:
    """A complete, valid last line missing only its newline is never destroyed."""
    reg = tmp / "reg.jsonl"
    vr.append({"type": "family_proposed", "family_id": "f1", "source": "created",
               "members": [_m("a1")]}, path=reg)
    raw = reg.read_bytes()
    assert raw.endswith(b"\n")
    reg.write_bytes(raw[:-1])  # strip the trailing newline off an otherwise complete, valid line
    vr.append({"type": "family_proposed", "family_id": "f2", "source": "created",
               "members": [_m("a2")]}, path=reg)
    events, warnings = vr.read_events(reg)
    assert len(events) == 2 and not warnings, (len(events), warnings)
    torn_path = reg.with_name(reg.name + ".torn")
    assert not torn_path.exists(), "a complete, valid line must never be moved to .torn"


def _st_corrupt_middle_blocks(tmp: Path) -> None:
    reg = tmp / "reg.jsonl"
    reg.write_text('{"broken\n' + json.dumps({"schema_version": 1, "event_id": "e", "seq": 2,
                   "type": "family_confirmed", "family_id": "f1"}) + "\n", encoding="utf-8")
    try:
        vr.read_events(reg)
    except vr.RegistryCorrupt:
        return
    raise AssertionError("a corrupt middle line must raise")


def _st_one_actual_per_family(tmp: Path) -> None:
    ev = [_ev("family_proposed", family_id="f1", members=[_m("a1"), _m("a2")]),
          _ev("family_confirmed", family_id="f1"),
          _ev("decided", family_id="f1", artifact_id="a2", sha256=_m("a2")["sha256"])]
    st = vr.replay(ev)
    assert st.families["f1"].actual == "a2"


def _st_artifact_in_two_confirmed_families_is_corrupt(tmp: Path) -> None:
    ev = [_ev("family_proposed", family_id="f1", members=[_m("a1")]), _ev("family_confirmed", family_id="f1"),
          _ev("family_proposed", family_id="f2", members=[_m("a1")]), _ev("family_confirmed", family_id="f2")]
    try:
        vr.replay(ev)
    except vr.RegistryCorrupt:
        return
    raise AssertionError("invariant: one confirmed family per artifact")


def _st_rejection_keyed_by_content(tmp: Path) -> None:
    ev = [_ev("rejected", shas=[_m("a1")["sha256"], _m("a2")["sha256"]])]
    st = vr.replay(ev)
    assert frozenset([_m("a1")["sha256"], _m("a2")["sha256"]]) in st.rejections


def _st_locked_is_reentrant(tmp: Path) -> None:
    """A caller already holding `locked()` can call `append()` without deadlock."""
    reg = tmp / "reg.jsonl"
    with vr.locked(reg), vr.locked(reg):
        vr.append({"type": "family_proposed", "family_id": "f1", "source": "created",
                   "members": [_m("a1")]}, path=reg)
    events, warnings = vr.read_events(reg)
    assert len(events) == 1 and not warnings, warnings


def _st_append_refuses_invariant_break(tmp: Path) -> None:
    """`append` replays the prospective event first and writes nothing on refusal."""
    reg = tmp / "reg.jsonl"
    vr.append({"type": "family_proposed", "family_id": "f1", "source": "created", "members": [_m("a1")]}, path=reg)
    vr.append({"type": "family_confirmed", "family_id": "f1"}, path=reg)
    vr.append({"type": "family_proposed", "family_id": "f2", "source": "created", "members": [_m("a1")]}, path=reg)
    before = reg.read_text(encoding="utf-8")
    try:
        vr.append({"type": "family_confirmed", "family_id": "f2"}, path=reg)
    except vr.RegistryCorrupt:
        after = reg.read_text(encoding="utf-8")
        assert after == before, "a refused append must leave the file byte-for-byte untouched"
        return
    raise AssertionError("append must refuse an event that breaks an invariant")


def _st_random_sequences_keep_invariants(tmp: Path) -> None:
    """A few hundred random events through `append`; the log must always replay clean."""
    reg = tmp / "reg.jsonl"
    rng = random.Random(20260930)  # noqa: S311 - deterministic sequence, not cryptographic
    family_ids = [f"fam{i}" for i in range(1, 6)]
    artifact_ids = [f"art{i}" for i in range(1, 9)]
    kinds = ("family_proposed", "family_confirmed", "decided", "member_removed", "rejected")
    refused = 0
    for _ in range(300):
        fid = rng.choice(family_ids)
        kind = rng.choice(kinds)
        if kind == "family_proposed":
            picked = rng.sample(artifact_ids, rng.randint(1, 3))
            event = {"type": "family_proposed", "family_id": fid, "source": "created",
                     "members": [_m(aid) for aid in picked]}
        elif kind == "family_confirmed":
            event = {"type": "family_confirmed", "family_id": fid}
        elif kind == "decided":
            aid = rng.choice(artifact_ids)
            event = {"type": "decided", "family_id": fid, "artifact_id": aid, "sha256": _m(aid)["sha256"]}
        elif kind == "member_removed":
            aid = rng.choice(artifact_ids)
            event = {"type": "member_removed", "family_id": fid, "artifact_id": aid}
        else:
            shas = [_m(aid)["sha256"] for aid in rng.sample(artifact_ids, 2)]
            event = {"type": "rejected", "shas": shas}
        try:
            vr.append(event, path=reg)
        except vr.RegistryCorrupt:
            refused += 1
            continue
        events, warnings = vr.read_events(reg)
        assert not warnings, warnings
        st = vr.replay(events)
        owner: dict[str, str] = {}
        for fam in st.families.values():
            if not fam.confirmed:
                continue
            for aid in fam.members:
                assert aid not in owner, f"artifact {aid} is in two confirmed families"
                owner[aid] = fam.family_id
            if fam.actual is not None:
                assert fam.actual in fam.members, f"actual {fam.actual} is not a confirmed member of {fam.family_id}"
    print(f"     ({refused} RegistryCorrupt refusals swallowed of 300 attempts)")


def _r4_worker(reg: str, n: int, actor: str) -> None:
    p = Path(reg)
    for i in range(n):
        vr.append({"type": "family_proposed", "family_id": f"{actor}-{i}", "source": "created",
                   "members": [_m(f"{actor}-a{i}")]}, path=p)


def _st_concurrent_appends_keep_order(tmp: Path) -> None:
    """Two processes append to the same registry; the result has 2N ordered events."""
    reg = tmp / "reg.jsonl"
    n = 25
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_r4_worker, args=(str(reg), n, actor)) for actor in ("p1", "p2")]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=60)
        assert proc.exitcode == 0, f"worker exited with {proc.exitcode!r}"
    events, warnings = vr.read_events(reg)
    assert not warnings, warnings
    assert len(events) == 2 * n, len(events)
    seqs = [e["seq"] for e in events]
    assert len(set(seqs)) == len(seqs), "seq values must be unique"
    assert seqs == sorted(seqs), "seq values must be strictly increasing in file order"
    assert seqs == list(range(1, 2 * n + 1)), seqs


def _st_record_creates_proposed_family(tmp: Path) -> None:
    root, reg = _tree(tmp, {"d/a-v1.md": "one", "d/a-v2.md": "two"}), tmp / "reg.jsonl"
    rc = main(["record", "d/a-v1.md", "d/a-v2.md", "--root", str(root), "--registry", str(reg)])
    st = vr.replay(vr.read_events(reg)[0])
    fam = next(iter(st.families.values()))
    assert rc == 0 and not fam.confirmed and len(fam.proposed) == 2


def _st_decide_refuses_changed_revision(tmp: Path) -> None:
    root, reg = _tree(tmp, {"d/a-v1.md": "one", "d/a-v2.md": "two"}), tmp / "reg.jsonl"
    main(["record", "d/a-v1.md", "d/a-v2.md", "--root", str(root), "--registry", str(reg)])
    fid = next(iter(vr.replay(vr.read_events(reg)[0]).families))
    main(["confirm", fid, "--registry", str(reg)])
    shown = hashlib.sha256(b"two").hexdigest()
    (root / "d/a-v2.md").write_text("two, edited", encoding="utf-8")
    rc = main(["decide", fid, "2", "--expect-sha", shown, "--root", str(root), "--registry", str(reg)])
    assert rc != 0 and vr.replay(vr.read_events(reg)[0]).families[fid].actual is None


def _st_redecide_is_the_revert(tmp: Path) -> None:
    root, reg = _tree(tmp, {"d/a-v1.md": "one", "d/a-v2.md": "two"}), tmp / "reg.jsonl"
    main(["record", "d/a-v1.md", "d/a-v2.md", "--root", str(root), "--registry", str(reg)])
    fid = next(iter(vr.replay(vr.read_events(reg)[0]).families))
    main(["confirm", fid, "--registry", str(reg)])
    _decide(root, reg, fid, "2")
    _decide(root, reg, fid, "1")
    fam = vr.replay(vr.read_events(reg)[0]).families[fid]
    assert fam.members[fam.actual].path == "d/a-v1.md"


def _st_decide_refuses_unconfirmed_family(tmp: Path) -> None:
    root, reg = _tree(tmp, {"d/a-v1.md": "one", "d/a-v2.md": "two"}), tmp / "reg.jsonl"
    main(["record", "d/a-v1.md", "d/a-v2.md", "--root", str(root), "--registry", str(reg)])
    fid = next(iter(vr.replay(vr.read_events(reg)[0]).families))
    rc = main(["decide", fid, "1", "--expect-sha", hashlib.sha256(b"one").hexdigest(),
               "--root", str(root), "--registry", str(reg)])
    assert rc != 0
    assert vr.replay(vr.read_events(reg)[0]).families[fid].actual is None


def _st_decide_proposed_member_confirms_it(tmp: Path) -> None:
    root, reg = _tree(tmp, {"d/a-v1.md": "one", "d/a-v2.md": "two", "d/a-v3.md": "three"}), tmp / "reg.jsonl"
    main(["record", "d/a-v1.md", "d/a-v2.md", "--root", str(root), "--registry", str(reg)])
    fid = next(iter(vr.replay(vr.read_events(reg)[0]).families))
    main(["confirm", fid, "--registry", str(reg)])
    aid3 = uuid.uuid4().hex
    vr.append({"type": "member_proposed", "family_id": fid,
               "member": {"artifact_id": aid3, "path": "d/a-v3.md",
                          "sha256": hashlib.sha256(b"three").hexdigest()}}, path=reg)
    rc = _decide(root, reg, fid, "3")
    fam = vr.replay(vr.read_events(reg)[0]).families[fid]
    assert rc == 0 and fam.actual == aid3 and aid3 in fam.members and aid3 not in fam.proposed


def _st_show_marks_changed_after_decision(tmp: Path) -> None:
    root, reg = _tree(tmp, {"d/a-v1.md": "one", "d/a-v2.md": "two"}), tmp / "reg.jsonl"
    main(["record", "d/a-v1.md", "d/a-v2.md", "--root", str(root), "--registry", str(reg)])
    fid = next(iter(vr.replay(vr.read_events(reg)[0]).families))
    main(["confirm", fid, "--registry", str(reg)])
    _decide(root, reg, fid, "2")
    (root / "d/a-v2.md").write_text("two, edited", encoding="utf-8")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(["show", fid, "--root", str(root), "--registry", str(reg)])
    out = buf.getvalue()
    assert rc == 0 and "changed after decision" in out, out


def _st_summary_stored_for_pair(tmp: Path) -> None:
    root, reg = _tree(tmp, {"d/a-v1.md": "one", "d/a-v2.md": "two"}), tmp / "reg.jsonl"
    main(["record", "d/a-v1.md", "d/a-v2.md", "--root", str(root), "--registry", str(reg)])
    fid = next(iter(vr.replay(vr.read_events(reg)[0]).families))
    main(["confirm", fid, "--registry", str(reg)])
    rc = main(["summary", fid, "1", "2", "added a section", "--root", str(root), "--registry", str(reg)])
    fam = vr.replay(vr.read_events(reg)[0]).families[fid]
    sha1 = hashlib.sha256(b"one").hexdigest()
    sha2 = hashlib.sha256(b"two").hexdigest()
    assert rc == 0 and fam.summaries[(sha1, sha2)] == "added a section"


def _st_record_refuses_path_in_confirmed_family(tmp: Path) -> None:
    """A path already confirmed elsewhere must not seed a dead-end family."""
    root, reg = _tree(tmp, {"d/a-v1.md": "one", "d/a-v2.md": "two", "d/a-v3.md": "three"}), tmp / "reg.jsonl"
    main(["record", "d/a-v1.md", "d/a-v2.md", "--root", str(root), "--registry", str(reg)])
    fid = next(iter(vr.replay(vr.read_events(reg)[0]).families))
    main(["confirm", fid, "--registry", str(reg)])
    rc = main(["record", "d/a-v2.md", "d/a-v3.md", "--root", str(root), "--registry", str(reg)])
    st = vr.replay(vr.read_events(reg)[0])
    assert rc != 0, "a path already confirmed elsewhere must refuse, not propose a second family"
    assert len(st.families) == 1, "no dead-end second family may be created"


def _st_reject_family_keys_by_content(tmp: Path) -> None:
    """`reject` by family id and `reject` by paths both key by content SHA-256."""
    root, reg = _tree(tmp, {"d/a-v1.md": "one", "d/a-v2.md": "two"}), tmp / "reg.jsonl"
    main(["record", "d/a-v1.md", "d/a-v2.md", "--root", str(root), "--registry", str(reg)])
    fid = next(iter(vr.replay(vr.read_events(reg)[0]).families))
    rc = main(["reject", fid, "--root", str(root), "--registry", str(reg)])
    sha1 = hashlib.sha256(b"one").hexdigest()
    sha2 = hashlib.sha256(b"two").hexdigest()
    st = vr.replay(vr.read_events(reg)[0])
    assert rc == 0 and frozenset({sha1, sha2}) in st.rejections

    (root / "d/x1.md").write_text("dup", encoding="utf-8")
    (root / "d/x2.md").write_text("dup", encoding="utf-8")
    rc2 = main(["reject", "d/x1.md", "d/x2.md", "--root", str(root), "--registry", str(reg)])
    shadup = hashlib.sha256(b"dup").hexdigest()
    st2 = vr.replay(vr.read_events(reg)[0])
    assert rc2 == 0 and frozenset({shadup}) in st2.rejections


def _st_remove_member_of_actual_clears_it(tmp: Path) -> None:
    """Removing the family's current actual clears `actual`/`actual_sha`."""
    root, reg = _tree(tmp, {"d/a-v1.md": "one", "d/a-v2.md": "two"}), tmp / "reg.jsonl"
    main(["record", "d/a-v1.md", "d/a-v2.md", "--root", str(root), "--registry", str(reg)])
    fid = next(iter(vr.replay(vr.read_events(reg)[0]).families))
    main(["confirm", fid, "--registry", str(reg)])
    _decide(root, reg, fid, "2")
    assert vr.replay(vr.read_events(reg)[0]).families[fid].actual is not None
    rc = main(["remove-member", fid, "2", "--registry", str(reg)])
    fam = vr.replay(vr.read_events(reg)[0]).families[fid]
    assert rc == 0 and fam.actual is None and fam.actual_sha is None


def _st_twin_follows_only_matching_sha(tmp: Path) -> None:
    """A twin is valid exactly while its header's SHA-256 matches the original.

    `sample/draft` is an INVENTED root, never one of the nine real family
    roots: `in_scope`'s own self-tests always pass a `roots` argument (or, for
    `twin_of`/`twin_valid`/`text_for`, need no root at all beyond the caller's
    own `root` fixture), so nothing here depends on, or can touch, the real
    data repository.
    """
    root = tmp / "root"
    (root / "sample/draft").mkdir(parents=True)
    original = root / "sample/draft/report.pdf"
    original.write_bytes(b"original bytes")
    sha = hashlib.sha256(b"original bytes").hexdigest()
    twin = root / "sample/draft/report-extract.md"
    twin.write_text(
        f"# Extract: report.pdf\n\n> Source SHA-256: {sha}; converter: liteparse 1.0\n\ntext\n",
        encoding="utf-8")
    assert doc_scope.twin_valid(root, "sample/draft/report.pdf")
    assert doc_scope.text_for(root, "sample/draft/report.pdf") is not None
    original.write_bytes(b"edited bytes")
    assert not doc_scope.twin_valid(root, "sample/draft/report.pdf")
    assert doc_scope.text_for(root, "sample/draft/report.pdf") is None


def _st_scope_excludes_code_and_archive(tmp: Path) -> None:
    """Code, archives and service records are out; a tracked draft is in."""
    roots = ("sample/datastore", "sample/deliverable", "sample/draft")
    assert not doc_scope.in_scope("scripts/x.py", roots=roots)
    assert not doc_scope.in_scope("_archive/a.md", roots=roots)  # leak-guard: ok (an invented archive path the scope must refuse)
    assert not doc_scope.in_scope("sample/draft/trace/x.md", roots=roots)
    assert doc_scope.in_scope("sample/draft/a.docx", roots=roots)
    # The three exclusion reasons above each hold OUTSIDE every root too, so
    # this also confirms each one refuses even a file that IS otherwise in a
    # tracked root, per craft-silent-failure's "make the guard actually refuse".
    assert not doc_scope.in_scope("sample/draft/_archive/a.md", roots=roots)
    assert not doc_scope.in_scope("sample/draft/crm/a.md", roots=roots)
    assert not doc_scope.in_scope("sample/deliverable/runtime-junk/a.pdf", roots=roots)
    # HOS-195: the working folder beside a document, at any depth, by exact name only
    assert not doc_scope.in_scope("sample/draft/_work/a.docx", roots=roots)
    assert not doc_scope.in_scope("sample/draft/x/_work/y/a.md", roots=roots)
    assert doc_scope.in_scope("sample/draft/_workshop/a.docx", roots=roots)
    assert doc_scope.in_scope("sample/draft/_work.md", roots=roots)


def _st_twin_not_in_scope_itself(tmp: Path) -> None:
    """`report-extract.md` is never a family member in its own right."""
    roots = ("sample/draft",)
    assert not doc_scope.in_scope("sample/draft/report-extract.md", roots=roots)
    assert not doc_scope.in_scope("sample/draft/report-pdf-extract.md", roots=roots)


def _st_scope_rejects_escape_paths(tmp: Path) -> None:
    """An absolute path or a `..` climb is refused, never resolved and read."""
    roots = ("sample/draft",)
    assert not doc_scope.in_scope("/etc/passwd", roots=roots)
    assert not doc_scope.in_scope("sample/draft/../../escape.md", roots=roots)
    assert doc_scope.twin_of(tmp, "../escape.pdf") is None
    assert doc_scope.twin_valid(tmp, "../escape.pdf") is False
    assert doc_scope.text_for(tmp, "../escape.pdf") is None


def _st_dedupe_nested_drops_covered_roots(tmp: Path) -> None:
    """`_dedupe_nested` drops a duplicate root and a root already covered by an ancestor in the list."""
    deduped = doc_scope._dedupe_nested(
        ["sample/deliverable", "sample/deliverable/datastore", "a/b", "a/b", "c/d"])
    assert deduped == ("sample/deliverable", "a/b", "c/d"), deduped


# ============================================================
# Self-test scenarios (Task 4: scan availability, relocation, duplicates)
# ============================================================


def _confirmed_state(*members: dict) -> vr.State:
    """A single confirmed family holding `members` (each a member dict), replayed fresh."""
    events = [_ev("family_proposed", family_id="f1", source="created", members=list(members)),
              _ev("family_confirmed", family_id="f1")]
    return vr.replay(events)


def _st_partial_scan_deletes_nothing(tmp: Path) -> None:
    """A partial scan plans zero deletions, even for a path hidden by the very
    failure that made the pass incomplete, and even for one that is genuinely
    gone and unrelated to that failure.

    MEASURED 2026-10-01: the first version of this test used only ONE member
    (`a1`, `sample/draft/a.md`), a file that sat OUTSIDE the unreadable
    directory and so was found and hashed regardless of the failure. Removing
    the `if not result.complete: return [], prev_absent` guard from
    `availability_events` still passed that version, 34/34, because `a1`
    looked present either way and nothing else was absent to expose the
    difference. `b1` (inside the locked directory) and `c1` (never created at
    all) both look absent to a pass that finished normally; only the guard
    keeps them from producing an event.
    """
    root = tmp / "root"
    (root / "sample/draft").mkdir(parents=True)
    (root / "sample/draft/a.md").write_text("hello", encoding="utf-8")
    sha_a = hashlib.sha256(b"hello").hexdigest()
    sha_b = hashlib.sha256(b"inside the locked directory").hexdigest()
    sha_c = hashlib.sha256(b"deleted before this pass ever ran").hexdigest()
    cache = tmp / "cache.json"

    state = _confirmed_state(
        {"artifact_id": "a1", "path": "sample/draft/a.md", "sha256": sha_a},
        {"artifact_id": "b1", "path": "sample/draft/locked/b.md", "sha256": sha_b},
        {"artifact_id": "c1", "path": "sample/draft/c.md", "sha256": sha_c},
    )

    if os.geteuid() == 0:
        # root ignores a directory's permission bits, so chmod 000 would not
        # actually block the walk here. A missing scope root is refused for
        # the identical reason (an OSError-worthy condition this pass cannot
        # see past), and is the one that still holds under root; b1's and
        # c1's files are simply never created, so both look absent either way.
        result = doc_scan.scan(root, cache, roots=("sample/draft", "sample/missing-under-root"),
                                write_cache=False)
    else:
        locked = root / "sample/draft/locked"
        locked.mkdir()
        (locked / "b.md").write_text("inside the locked directory", encoding="utf-8")
        original_mode = locked.stat().st_mode
        os.chmod(locked, 0o000)
        try:
            result = doc_scan.scan(root, cache, roots=("sample/draft",), write_cache=False)
        finally:
            os.chmod(locked, original_mode)

    assert result.complete is False, "an unreadable directory must mark the pass incomplete"
    assert result.errors, "the incompleteness must be explained, not silent"

    events, new_absent = doc_scan.availability_events(state, set(), result)
    assert events == [] and new_absent == set(), (
        "a partial scan must plan zero availability changes: not for a1 (present), "
        "not for b1 (hidden by the very failure), and not for c1 (genuinely gone but "
        "unknowable as such from an incomplete pass)")


def _st_missing_root_is_incomplete(tmp: Path) -> None:
    """A scope root that does not exist at all makes the pass incomplete."""
    root = tmp / "root"
    (root / "sample/draft").mkdir(parents=True)
    (root / "sample/draft/a.md").write_text("hello", encoding="utf-8")
    cache = tmp / "cache.json"
    result = doc_scan.scan(root, cache, roots=("sample/draft", "sample/never-made"),
                            write_cache=False)
    assert result.complete is False
    assert any("sample/never-made" in e for e in result.errors), result.errors
    # The root that DOES exist must still be walked and hashed.
    assert result.files.get("sample/draft/a.md") == hashlib.sha256(b"hello").hexdigest()


def _st_missing_needs_two_scans(tmp: Path) -> None:
    """One absent complete scan is `unavailable`; a second consecutive one is `missing`."""
    root = tmp / "root"
    (root / "sample/draft").mkdir(parents=True)
    f = root / "sample/draft/a.md"
    f.write_text("hello", encoding="utf-8")
    sha = hashlib.sha256(b"hello").hexdigest()
    cache = tmp / "cache.json"
    state = _confirmed_state({"artifact_id": "a1", "path": "sample/draft/a.md", "sha256": sha})

    present_result = doc_scan.scan(root, cache, roots=("sample/draft",))
    events0, absent0 = doc_scan.availability_events(state, set(), present_result)
    assert events0 == [] and absent0 == set()

    f.unlink()
    first_absent = doc_scan.scan(root, cache, roots=("sample/draft",))
    events1, absent1 = doc_scan.availability_events(state, absent0, first_absent)
    assert events1 == [{"type": "unavailable", "artifact_id": "a1"}], events1
    assert absent1 == {"a1"}

    # Only after this event is appended does the registry's own state move to
    # "unavailable"; the next call reflects that with a fresh replay.
    unavailable_state = _confirmed_state_plus_unavailable()
    second_absent = doc_scan.scan(root, cache, roots=("sample/draft",))
    events2, absent2 = doc_scan.availability_events(unavailable_state, absent1, second_absent)
    assert events2 == [{"type": "missing", "artifact_id": "a1"}], events2
    assert absent2 == set(), "a member that just became missing must leave the unavailable set"


def _confirmed_state_plus_unavailable() -> vr.State:
    """The `a1`/`sample/draft/a.md` family, replayed as if `unavailable` had already
    been appended for it: mirrors what the registry holds once a caller has stored a
    previous `availability_events` call's proposed event."""
    events = [_ev("family_proposed", family_id="f1", source="created",
                   members=[{"artifact_id": "a1", "path": "sample/draft/a.md",
                             "sha256": hashlib.sha256(b"hello").hexdigest()}]),
              _ev("family_confirmed", family_id="f1"),
              _ev("unavailable", artifact_id="a1")]
    return vr.replay(events)


def _st_available_when_it_reappears(tmp: Path) -> None:
    """A member marked unavailable, then found present again, becomes `available`."""
    root = tmp / "root"
    (root / "sample/draft").mkdir(parents=True)
    f = root / "sample/draft/a.md"
    cache = tmp / "cache.json"
    unavailable_state = _confirmed_state_plus_unavailable()

    f.write_text("hello", encoding="utf-8")
    result = doc_scan.scan(root, cache, roots=("sample/draft",))
    events, new_absent = doc_scan.availability_events(unavailable_state, {"a1"}, result)
    assert events == [{"type": "available", "artifact_id": "a1"}], events
    assert new_absent == set()


def _st_relocated_when_unambiguous(tmp: Path) -> None:
    """Old path vanished, exactly one new in-scope path carries the same content: a move.

    A first scan runs BEFORE the rename to build a usable cache: "new path
    appeared in the SAME pass" requires a baseline of what already existed,
    per `_st_relocation_needs_new_path` below. A short sleep separates the
    cache write from the rename so `b.md`'s post-rename ctime is unambiguously
    later than the cache's `written_at` (an `os.rename` bumps ctime and leaves
    mtime untouched, MEASURED empirically), clear of any single-tick collision
    on a fast machine.
    """
    root = tmp / "root"
    (root / "sample/draft").mkdir(parents=True)
    (root / "sample/draft/a.md").write_text("hello", encoding="utf-8")
    sha = hashlib.sha256(b"hello").hexdigest()
    cache = tmp / "cache.json"
    state = _confirmed_state({"artifact_id": "a1", "path": "sample/draft/a.md", "sha256": sha})

    doc_scan.scan(root, cache, roots=("sample/draft",))
    time.sleep(0.02)

    (root / "sample/draft/a.md").rename(root / "sample/draft/b.md")
    result = doc_scan.scan(root, cache, roots=("sample/draft",))
    assert result.duplicates == {}
    events, new_absent = doc_scan.availability_events(state, set(), result)
    assert events == [{"type": "relocated", "artifact_id": "a1",
                        "path": "sample/draft/b.md", "sha256": sha}], events
    assert new_absent == set()


def _st_relocation_needs_new_path(tmp: Path) -> None:
    """A long-standing file sharing a vanished member's SHA-256 is not a target.

    `old.md` already existed, with the identical content, in the FIRST scan
    (the one that built the cache `a1` will later vanish against). When `a.md`
    then disappears, `old.md` is the one and only file left carrying that
    SHA-256 and is not any member's own recorded path, which is exactly what
    the pre-fix, three-condition check treated as an unambiguous relocation.
    It is not: `old.md` did not newly appear in this pass, so `a1` must take
    the ordinary unavailable route instead.
    """
    root = tmp / "root"
    (root / "sample/draft").mkdir(parents=True)
    (root / "sample/draft/a.md").write_text("hello", encoding="utf-8")
    (root / "sample/draft/old.md").write_text("hello", encoding="utf-8")
    sha = hashlib.sha256(b"hello").hexdigest()
    cache = tmp / "cache.json"
    state = _confirmed_state({"artifact_id": "a1", "path": "sample/draft/a.md", "sha256": sha})

    doc_scan.scan(root, cache, roots=("sample/draft",))  # old.md enters the cache here

    (root / "sample/draft/a.md").unlink()
    result = doc_scan.scan(root, cache, roots=("sample/draft",))
    events, new_absent = doc_scan.availability_events(state, set(), result)
    assert events == [{"type": "unavailable", "artifact_id": "a1"}], events
    assert new_absent == {"a1"}


def _st_no_usable_cache_never_relocates(tmp: Path) -> None:
    """With no usable previous cache at all, nothing this pass sees can be
    shown to be new, so relocation never fires on the very first scan."""
    root = tmp / "root"
    (root / "sample/draft").mkdir(parents=True)
    (root / "sample/draft/a.md").write_text("hello", encoding="utf-8")
    sha = hashlib.sha256(b"hello").hexdigest()
    cache = tmp / "cache.json"
    state = _confirmed_state({"artifact_id": "a1", "path": "sample/draft/a.md", "sha256": sha})

    (root / "sample/draft/a.md").rename(root / "sample/draft/b.md")
    result = doc_scan.scan(root, cache, roots=("sample/draft",), write_cache=False)
    assert result.cache_paths_at_start is None, "no cache file has ever been written yet"
    events, new_absent = doc_scan.availability_events(state, set(), result)
    assert events == [{"type": "unavailable", "artifact_id": "a1"}], events
    assert new_absent == {"a1"}


def _st_two_copies_are_duplicates_not_move(tmp: Path) -> None:
    """Two new files sharing the vanished member's SHA-256 are duplicates, never a move."""
    root = tmp / "root"
    (root / "sample/draft").mkdir(parents=True)
    (root / "sample/draft/a.md").write_text("hello", encoding="utf-8")
    sha = hashlib.sha256(b"hello").hexdigest()
    cache = tmp / "cache.json"
    state = _confirmed_state({"artifact_id": "a1", "path": "sample/draft/a.md", "sha256": sha})

    (root / "sample/draft/a.md").unlink()
    (root / "sample/draft/b.md").write_text("hello", encoding="utf-8")
    (root / "sample/draft/c.md").write_text("hello", encoding="utf-8")
    result = doc_scan.scan(root, cache, roots=("sample/draft",))
    assert result.duplicates == {sha: ["sample/draft/b.md", "sample/draft/c.md"]}, result.duplicates

    events, new_absent = doc_scan.availability_events(state, set(), result)
    assert events == [{"type": "unavailable", "artifact_id": "a1"}], events
    assert new_absent == {"a1"}


def _st_changed_during_walk_not_absent(tmp: Path) -> None:
    """A file rewritten mid-walk is skipped, and a skip is never reported as an absence."""
    root = tmp / "root"
    (root / "sample/draft").mkdir(parents=True)
    f = root / "sample/draft/a.md"
    f.write_text("hello", encoding="utf-8")
    sha = hashlib.sha256(b"hello").hexdigest()
    cache = tmp / "cache.json"

    original_hash = doc_scan._sha256_file

    def _hash_then_rewrite(path: Path, _orig=original_hash) -> str:
        digest = _orig(path)
        if path.name == "a.md":
            with path.open("a", encoding="utf-8") as fh:
                fh.write("!")  # a write landing between the two stats
            future = path.stat().st_mtime + 60
            os.utime(path, (future, future))
        return digest

    doc_scan._sha256_file = _hash_then_rewrite
    try:
        result = doc_scan.scan(root, cache, roots=("sample/draft",))
    finally:
        doc_scan._sha256_file = original_hash

    assert result.complete is True, "a skip is ordinary and must not mark the pass incomplete"
    assert "sample/draft/a.md" in result.skipped
    assert "sample/draft/a.md" not in result.files

    state = _confirmed_state({"artifact_id": "a1", "path": "sample/draft/a.md", "sha256": sha})
    events, new_absent = doc_scan.availability_events(state, set(), result)
    assert events == [] and new_absent == set(), (events, new_absent)


def _st_cache_reuses_unchanged_hash(tmp: Path) -> None:
    """A file whose (size, mtime_ns) is unchanged since the cache is not re-hashed."""
    root = tmp / "root"
    (root / "sample/draft").mkdir(parents=True)
    (root / "sample/draft/a.md").write_text("hello", encoding="utf-8")
    cache = tmp / "cache.json"

    calls: list[Path] = []
    original_hash = doc_scan._sha256_file

    def _counting_hash(path: Path, _orig=original_hash) -> str:
        calls.append(path)
        return _orig(path)

    doc_scan._sha256_file = _counting_hash
    try:
        first = doc_scan.scan(root, cache, roots=("sample/draft",))
        assert len(calls) == 1, "the first pass has nothing to reuse and must hash once"
        second = doc_scan.scan(root, cache, roots=("sample/draft",))
    finally:
        doc_scan._sha256_file = original_hash

    assert len(calls) == 1, "an unchanged (size, mtime_ns) must be read from the cache, not re-hashed"
    assert first.files == second.files


# ============================================================
# Self-test scenarios (Task 4 review, fix round 1)
# ============================================================


def _st_two_absent_members_share_sha_not_relocated(tmp: Path) -> None:
    """Two members recorded with identical content: both vanishing, with only
    one new file sharing that content, is ambiguous, never a move for either.

    MEASURED 2026-10-01: the pre-fix per-member relocation check treated each
    member independently, so both `a1` and `b1` were reported `relocated` to
    the single new `c.md`, which cannot both be true.
    """
    root = tmp / "root"
    (root / "sample/draft").mkdir(parents=True)
    (root / "sample/draft/a.md").write_text("same content", encoding="utf-8")
    (root / "sample/draft/b.md").write_text("same content", encoding="utf-8")
    sha = hashlib.sha256(b"same content").hexdigest()
    cache = tmp / "cache.json"
    state = _confirmed_state(
        {"artifact_id": "a1", "path": "sample/draft/a.md", "sha256": sha},
        {"artifact_id": "b1", "path": "sample/draft/b.md", "sha256": sha},
    )

    doc_scan.scan(root, cache, roots=("sample/draft",))  # establish a usable cache

    (root / "sample/draft/a.md").unlink()
    (root / "sample/draft/b.md").unlink()
    (root / "sample/draft/c.md").write_text("same content", encoding="utf-8")
    result = doc_scan.scan(root, cache, roots=("sample/draft",))
    events, new_absent = doc_scan.availability_events(state, set(), result)

    kinds = {e["artifact_id"]: e["type"] for e in events}
    assert kinds == {"a1": "unavailable", "b1": "unavailable"}, kinds
    assert new_absent == {"a1", "b1"}


def _st_default_roots_raises_not_silent(tmp: Path) -> None:
    """A data-root failure raises rather than returning an empty tuple, and is
    never cached: the mock is called again on the very next attempt."""
    doc_scope._default_roots.cache_clear()
    original_get_data_root = doc_scope.get_data_root
    calls: list[int] = []

    def _boom():
        calls.append(1)
        raise doc_scan.DataRootError("no data root for this test")

    doc_scope.get_data_root = _boom
    try:
        for _ in range(2):
            try:
                doc_scope._default_roots()
            except doc_scan.DataRootError:
                pass
            else:
                raise AssertionError("a failed data root must raise, not return ()")
    finally:
        doc_scope.get_data_root = original_get_data_root
        doc_scope._default_roots.cache_clear()

    assert len(calls) == 2, "a failed resolution must not be cached; each call must try again"


def _st_default_roots_rejects_out_of_root_candidate(tmp: Path) -> None:
    """A resolved candidate outside the data root is an error, never a silent drop."""
    doc_scope._default_roots.cache_clear()
    original_get_data_root = doc_scope.get_data_root
    original_data_path = doc_scope.data_path
    fake_root = tmp / "fake-data-root"
    fake_root.mkdir()
    outside = tmp / "outside-the-root"
    outside.mkdir()

    doc_scope.get_data_root = lambda: fake_root
    doc_scope.data_path = lambda name: outside  # every logical name resolves OUTSIDE fake_root
    try:
        try:
            doc_scope._default_roots()
        except doc_scope.DataPathError:
            pass
        else:
            raise AssertionError("a candidate outside the data root must raise, never vanish")
    finally:
        doc_scope.get_data_root = original_get_data_root
        doc_scope.data_path = original_data_path
        doc_scope._default_roots.cache_clear()


def _st_scan_catches_root_resolution_failure(tmp: Path) -> None:
    """A raised root-resolution failure is caught by `scan`, never crashes it."""
    root = tmp / "root"
    root.mkdir()
    cache = tmp / "cache.json"
    original_resolve_roots = doc_scan.doc_scope._resolve_roots

    def _boom(roots):
        raise doc_scan.DataRootError("no data root for this test")

    doc_scan.doc_scope._resolve_roots = _boom
    try:
        result = doc_scan.scan(root, cache, roots=None, write_cache=False)
    finally:
        doc_scan.doc_scope._resolve_roots = original_resolve_roots

    assert result.complete is False
    assert result.errors, "the failure must be explained, not silently swallowed"


def _st_empty_root_set_is_incomplete(tmp: Path) -> None:
    """An explicitly empty scope-root set must not read as a clean, complete pass."""
    root = tmp / "root"
    root.mkdir()
    cache = tmp / "cache.json"
    result = doc_scan.scan(root, cache, roots=(), write_cache=False)
    assert result.complete is False
    assert result.errors


def _st_out_of_scope_member_gets_no_event(tmp: Path) -> None:
    """A member recorded outside the roots this pass used gets no event at all,
    and is listed in `ScanResult.out_of_scope` instead."""
    root = tmp / "root"
    (root / "sample/draft").mkdir(parents=True)
    (root / "sample/draft/a.md").write_text("hello", encoding="utf-8")
    cache = tmp / "cache.json"

    sha_a = hashlib.sha256(b"hello").hexdigest()
    sha_outside = hashlib.sha256(b"filed where this pass never looks").hexdigest()
    state = _confirmed_state(
        {"artifact_id": "a1", "path": "sample/draft/a.md", "sha256": sha_a},
        {"artifact_id": "z1", "path": "sample/other-root/z.md", "sha256": sha_outside},
    )

    result = doc_scan.scan(root, cache, roots=("sample/draft",))
    events, new_absent = doc_scan.availability_events(state, set(), result)

    assert events == [], events
    assert new_absent == set()
    assert result.out_of_scope == ["z1"], result.out_of_scope


def _st_dedupes_members_by_artifact_id(tmp: Path) -> None:
    """The same artifact_id proposed in two different families gets one event, not two."""
    root = tmp / "root"
    (root / "sample/draft").mkdir(parents=True)
    sha = hashlib.sha256(b"same content, shared artifact").hexdigest()
    cache = tmp / "cache.json"

    events0 = [
        _ev("family_proposed", family_id="f1", source="created",
             members=[{"artifact_id": "shared1", "path": "sample/draft/a.md", "sha256": sha}]),
        _ev("family_confirmed", family_id="f1"),
        _ev("family_proposed", family_id="f2", source="created",
             members=[{"artifact_id": "shared1", "path": "sample/draft/a.md", "sha256": sha}]),
    ]
    state = vr.replay(events0)

    result = doc_scan.scan(root, cache, roots=("sample/draft",))  # a.md never created: absent
    events, new_absent = doc_scan.availability_events(state, set(), result)

    assert events == [{"type": "unavailable", "artifact_id": "shared1"}], events
    assert new_absent == {"shared1"}


def _st_symlinked_directory_is_incomplete(tmp: Path) -> None:
    """A symlinked directory under a scope root is refused, not silently walked past."""
    root = tmp / "root"
    (root / "sample/draft").mkdir(parents=True)
    (root / "sample/draft/a.md").write_text("hello", encoding="utf-8")
    outside_target = tmp / "outside-target"
    outside_target.mkdir()
    (outside_target / "hidden.md").write_text("hidden content", encoding="utf-8")
    (root / "sample/draft/linked").symlink_to(outside_target, target_is_directory=True)
    cache = tmp / "cache.json"

    result = doc_scan.scan(root, cache, roots=("sample/draft",), write_cache=False)

    assert result.complete is False, "a symlinked directory must mark the pass incomplete"
    assert any("linked" in e for e in result.errors), result.errors
    assert "sample/draft/linked/hidden.md" not in result.files


def _st_resolve_roots_dedupes_explicit_nested(tmp: Path) -> None:
    """Explicit roots passed by a caller are deduped and nesting-collapsed too,
    not only the real defaults."""
    resolved = doc_scope._resolve_roots(
        ["sample/draft", "sample/draft/nested", "sample/draft", "a/b"])
    assert resolved == ("sample/draft", "a/b"), resolved


def _st_cache_validates_sha_shape(tmp: Path) -> None:
    """A cache entry whose sha256 is not 64 lowercase hex characters is never
    trusted; the file is re-hashed instead."""
    root = tmp / "root"
    (root / "sample/draft").mkdir(parents=True)
    f = root / "sample/draft/a.md"
    f.write_text("hello", encoding="utf-8")
    cache = tmp / "cache.json"

    st = f.stat()
    cache.write_text(
        json.dumps({"written_at": time.time_ns(),
                    "files": {"sample/draft/a.md": [st.st_size, st.st_mtime_ns, "not-a-real-sha"]}}),
        encoding="utf-8")

    result = doc_scan.scan(root, cache, roots=("sample/draft",), write_cache=False)
    assert result.files["sample/draft/a.md"] == hashlib.sha256(b"hello").hexdigest()


# ============================================================
# Self-test scenarios (Task 4 re-review, fix round 2)
# ============================================================


def _st_relocation_not_fooled_by_skipped_file(tmp: Path) -> None:
    """A skipped file sharing a candidate's SHA-256 blocks relocation, even
    when the candidate is otherwise a perfectly valid, brand-new target.

    Satisfies both the standalone review finding on doc_scan.py's "no skipped
    file carrying the SHA" condition and the identically-named test in the
    cache-correctness ruling. `new.md` alone would pass every other
    condition (new path, new ctime, unclaimed, sole absent member): only the
    skipped-file check stands between it and a false `relocated`.
    """
    root = tmp / "root"
    (root / "sample/draft").mkdir(parents=True)
    (root / "sample/draft/a.md").write_text("hello", encoding="utf-8")
    sha = hashlib.sha256(b"hello").hexdigest()
    cache = tmp / "cache.json"
    state = _confirmed_state({"artifact_id": "a1", "path": "sample/draft/a.md", "sha256": sha})

    doc_scan.scan(root, cache, roots=("sample/draft",))
    time.sleep(0.02)

    (root / "sample/draft/a.md").unlink()
    (root / "sample/draft/new.md").write_text("hello", encoding="utf-8")
    (root / "sample/draft/changing.md").write_text("hello", encoding="utf-8")

    original_hash = doc_scan._sha256_file

    def _hash_then_rewrite(path: Path, _orig=original_hash) -> str:
        digest = _orig(path)
        if path.name == "changing.md":
            with path.open("a", encoding="utf-8") as fh:
                fh.write("!")
            future = path.stat().st_mtime + 60
            os.utime(path, (future, future))
        return digest

    doc_scan._sha256_file = _hash_then_rewrite
    try:
        result = doc_scan.scan(root, cache, roots=("sample/draft",))
    finally:
        doc_scan._sha256_file = original_hash

    assert "sample/draft/changing.md" in result.skipped
    assert result.skipped_shas.get("sample/draft/changing.md") == sha

    events, new_absent = doc_scan.availability_events(state, set(), result)
    assert events == [{"type": "unavailable", "artifact_id": "a1"}], events
    assert new_absent == {"a1"}


def _st_relocation_not_fooled_by_incomplete_pass(tmp: Path) -> None:
    """An incomplete pass must not drop a previously-cached path it simply did
    not visit this time; the very next pass must still know about it.

    `old.md` sits under `locked/`, which this test physically moves OUT of
    the walked tree during the "incomplete" pass (so it is genuinely not
    seen, no chmod and no root-privilege dependency needed), while the pass is
    made incomplete a DIFFERENT way (an extra scope root that does not
    exist), so the two causes stay independent and the test holds identically
    whether or not the process runs as root.
    """
    root = tmp / "root"
    (root / "sample/draft/locked").mkdir(parents=True)
    (root / "sample/draft/a.md").write_text("hello", encoding="utf-8")
    (root / "sample/draft/locked/old.md").write_text("something else", encoding="utf-8")
    cache = tmp / "cache.json"

    doc_scan.scan(root, cache, roots=("sample/draft",))  # complete: both paths now cached

    hidden_elsewhere = tmp / "locked-moved-aside"
    (root / "sample/draft/locked").rename(hidden_elsewhere)
    try:
        incomplete = doc_scan.scan(root, cache, roots=("sample/draft", "sample/never-made"))
    finally:
        hidden_elsewhere.rename(root / "sample/draft/locked")
    assert incomplete.complete is False

    resumed = doc_scan.scan(root, cache, roots=("sample/draft",), write_cache=False)
    assert "sample/draft/locked/old.md" in (resumed.cache_paths_at_start or frozenset()), (
        "an incomplete pass must persist union(previous keys, keys seen now), "
        "never drop a previous key")


def _st_relocation_not_fooled_by_failed_cache_write(tmp: Path) -> None:
    """A cache write that fails marks THIS pass incomplete; nothing it found
    is then trusted enough to act on."""
    root = tmp / "root"
    (root / "sample/draft").mkdir(parents=True)
    (root / "sample/draft/a.md").write_text("hello", encoding="utf-8")
    cache = tmp / "cache.json"

    original_write_cache = doc_scan._write_cache

    def _boom(path, files, written_at):
        raise OSError("simulated disk failure")

    doc_scan._write_cache = _boom
    try:
        result = doc_scan.scan(root, cache, roots=("sample/draft",))
    finally:
        doc_scan._write_cache = original_write_cache

    assert result.complete is False, "a failed cache write must mark this pass incomplete"
    assert result.errors

    state = _confirmed_state({"artifact_id": "a1", "path": "sample/draft/a.md",
                               "sha256": hashlib.sha256(b"hello").hexdigest()})
    events, new_absent = doc_scan.availability_events(state, set(), result)
    assert events == [] and new_absent == set(), (
        "a pass that failed only on writing its own cache must still plan nothing")


def _st_relocation_requires_new_ctime(tmp: Path) -> None:
    """A candidate that is new to the cache's key SET but whose own ctime
    predates the cache's `written_at` is still refused: it was never touched
    since before this baseline existed, so it cannot have just moved here.

    `old.md` sits outside the FIRST pass's scope roots entirely (so it never
    becomes a cache key at all, passing the path-based "new" check on a
    technicality), but it was created before that first pass ever ran and is
    never touched again; only widening the scope on the SECOND pass makes it
    visible. Its ctime is older than the cache's `written_at` regardless.
    """
    root = tmp / "root"
    (root / "sample/draft").mkdir(parents=True)
    (root / "sample/other").mkdir(parents=True)
    (root / "sample/draft/a.md").write_text("hello", encoding="utf-8")
    (root / "sample/other/old.md").write_text("hello", encoding="utf-8")
    sha = hashlib.sha256(b"hello").hexdigest()
    cache = tmp / "cache.json"
    state = _confirmed_state({"artifact_id": "a1", "path": "sample/draft/a.md", "sha256": sha})

    time.sleep(0.02)
    doc_scan.scan(root, cache, roots=("sample/draft",))  # old.md stays out of scope, never cached

    (root / "sample/draft/a.md").unlink()
    result = doc_scan.scan(root, cache, roots=("sample/draft", "sample/other"))
    assert "sample/other/old.md" not in (result.cache_paths_at_start or frozenset()), (
        "old.md must indeed look new to the cache's key set for this test to "
        "isolate the ctime check")

    events, new_absent = doc_scan.availability_events(state, set(), result)
    assert events == [{"type": "unavailable", "artifact_id": "a1"}], events
    assert new_absent == {"a1"}


def _late_absent_state(sha: str, seen_at: str) -> vr.State:
    """The `a1`/`sample/draft/a.md` family, last seen present at `seen_at`, then
    marked `unavailable` and `missing` by two complete passes."""
    events = [_ev("family_proposed", family_id="f1", source="created", ts=seen_at,
                   members=[{"artifact_id": "a1", "path": "sample/draft/a.md", "sha256": sha}]),
              _ev("family_confirmed", family_id="f1", ts=seen_at),
              _ev("unavailable", artifact_id="a1"),
              _ev("missing", artifact_id="a1")]
    return vr.replay(events)


def _iso_now(offset_s: float = 0.0) -> str:
    return datetime.fromtimestamp(time.time() + offset_s, tz=timezone.utc).isoformat()


def _st_absent_member_followed_late(tmp: Path) -> None:
    """A member already `missing` is followed to a path that only came into
    scope after it vanished, though that path is now an old cache key with a
    ctime older than the cache: its ctime is later than the member's `seen_at`.

    MEASURED 2026-10-12 (Part C pilot): without the late route, five moved
    members stayed `missing` for every pass after the scope widened.
    """
    root = tmp / "root"
    (root / "sample/draft").mkdir(parents=True)
    (root / "sample/moved").mkdir(parents=True)
    sha = hashlib.sha256(b"hello").hexdigest()
    cache = tmp / "cache.json"
    state = _late_absent_state(sha, _iso_now(-60))

    (root / "sample/moved/b.md").write_text("hello", encoding="utf-8")
    roots = ("sample/draft", "sample/moved")
    doc_scan.scan(root, cache, roots=roots)
    time.sleep(0.02)
    result = doc_scan.scan(root, cache, roots=roots)
    assert "sample/moved/b.md" in (result.cache_paths_at_start or frozenset()), (
        "b.md must already be a cache key, so only the late route can follow it")
    events, new_absent = doc_scan.availability_events(state, set(), result)
    assert events == [{"type": "relocated", "artifact_id": "a1",
                        "path": "sample/moved/b.md", "sha256": sha}], events
    assert new_absent == set()


def _st_late_follow_refuses_long_standing_copy(tmp: Path) -> None:
    """The late route refuses a file whose ctime predates the member's `seen_at`:
    a copy that already sat there while the member was still present is not
    where it moved to."""
    root = tmp / "root"
    (root / "sample/draft").mkdir(parents=True)
    (root / "sample/other").mkdir(parents=True)
    (root / "sample/other/old.md").write_text("hello", encoding="utf-8")
    sha = hashlib.sha256(b"hello").hexdigest()
    cache = tmp / "cache.json"
    time.sleep(0.02)
    state = _late_absent_state(sha, _iso_now())

    roots = ("sample/draft", "sample/other")
    doc_scan.scan(root, cache, roots=roots)
    result = doc_scan.scan(root, cache, roots=roots)
    events, new_absent = doc_scan.availability_events(state, set(), result)
    assert events == [] and new_absent == set(), (events, new_absent)


def _st_symlinked_parent_of_explicit_root_refused(tmp: Path) -> None:
    """A symlinked PARENT of a scope root, not just the leaf root itself, is refused."""
    root = tmp / "root"
    (root / "sample").mkdir(parents=True)
    outside = tmp / "outside-parent-target"
    (outside / "draft").mkdir(parents=True)
    (outside / "draft/a.md").write_text("hello", encoding="utf-8")
    (root / "sample/linked-parent").symlink_to(outside, target_is_directory=True)
    cache = tmp / "cache.json"

    result = doc_scan.scan(root, cache, roots=("sample/linked-parent/draft",), write_cache=False)

    assert result.complete is False, "a symlinked parent directory must mark the pass incomplete"
    assert any("linked-parent" in e for e in result.errors), result.errors
    assert "sample/linked-parent/draft/a.md" not in result.files


def _st_default_roots_refuses_resolved_symlink_swap(tmp: Path) -> None:
    """`_default_roots` must not let `.resolve()` silently swap a symlinked
    candidate for wherever it points; it raises instead.

    The symlink's target (`real-elsewhere`) is placed INSIDE `fake_root`, not
    outside it: the pre-existing "candidate outside the data root" guard would
    also raise on an outside target, which would pass this test for the wrong
    reason and never actually exercise the symlink-swap check on its own. With
    the target still under `fake_root`, the OLD "outside root" guard has
    nothing to catch, and only a dedicated symlink check can refuse this.
    """
    doc_scope._default_roots.cache_clear()
    original_get_data_root = doc_scope.get_data_root
    original_data_path = doc_scope.data_path
    fake_root = tmp / "fake-data-root"
    fake_root.mkdir()
    real_target = fake_root / "real-elsewhere"
    real_target.mkdir()
    symlinked_candidate = fake_root / "symlinked-candidate"
    symlinked_candidate.symlink_to(real_target, target_is_directory=True)

    doc_scope.get_data_root = lambda: fake_root
    doc_scope.data_path = lambda name: symlinked_candidate
    try:
        try:
            doc_scope._default_roots()
        except doc_scope.DataPathError:
            pass
        else:
            raise AssertionError(
                "a candidate reached only through a symlink must raise, never silently follow it")
    finally:
        doc_scope.get_data_root = original_get_data_root
        doc_scope.data_path = original_data_path
        doc_scope._default_roots.cache_clear()


def _st_out_of_scope_not_mutated_across_calls(tmp: Path) -> None:
    """Calling availability_events twice on the same ScanResult must not
    duplicate out_of_scope entries: the list is replaced, never appended to."""
    root = tmp / "root"
    (root / "sample/draft").mkdir(parents=True)
    cache = tmp / "cache.json"
    sha_outside = hashlib.sha256(b"outside").hexdigest()
    state = _confirmed_state(
        {"artifact_id": "z1", "path": "sample/other-root/z.md", "sha256": sha_outside})

    result = doc_scan.scan(root, cache, roots=("sample/draft",))
    doc_scan.availability_events(state, set(), result)
    doc_scan.availability_events(state, set(), result)

    assert result.out_of_scope == ["z1"], result.out_of_scope


def _st_scan_catches_runtime_error_during_root_resolution(tmp: Path) -> None:
    """A bare RuntimeError or OSError from root resolution is caught too, not
    just the two doc_scope-specific exception types."""
    root = tmp / "root"
    root.mkdir()
    cache = tmp / "cache.json"
    original_resolve_roots = doc_scan.doc_scope._resolve_roots

    def _boom(roots):
        raise RuntimeError("unexpected failure resolving roots")

    doc_scan.doc_scope._resolve_roots = _boom
    try:
        result = doc_scan.scan(root, cache, roots=None, write_cache=False)
    finally:
        doc_scan.doc_scope._resolve_roots = original_resolve_roots

    assert result.complete is False
    assert result.errors


# ============================================================
# Self-test scenarios (Task 4 re-review round 2, fix round 3)
# ============================================================


def _st_relocation_not_fooled_by_resaved_skipped_file(tmp: Path) -> None:
    """A file skipped on pass N, then resaved with the vanished member's
    content, must be recognised from pass N's cache entry as already known,
    not treated as a brand-new relocation target just because its ctime (from
    the resave) happens to postdate the cache's written_at.

    `x.md` is skipped on pass N (a write lands mid-hash) and, per case (e),
    kept in that pass's cache with a null hash rather than dropped. Right
    after pass N's cache is written, `x.md` is overwritten AGAIN, this time
    with `a1`'s own content, so its ctime is now strictly newer than
    `written_at`: the ctime defence alone would wave this through. Only the
    fact that `x.md` was ALREADY a cache key from pass N (case e) keeps
    `is_new_path` False and refuses the relocation.
    """
    root = tmp / "root"
    (root / "sample/draft").mkdir(parents=True)
    (root / "sample/draft/a.md").write_text("hello", encoding="utf-8")
    (root / "sample/draft/x.md").write_text("initial content", encoding="utf-8")
    sha = hashlib.sha256(b"hello").hexdigest()
    cache = tmp / "cache.json"
    state = _confirmed_state({"artifact_id": "a1", "path": "sample/draft/a.md", "sha256": sha})

    original_hash = doc_scan._sha256_file

    def _hash_then_rewrite(path: Path, _orig=original_hash) -> str:
        digest = _orig(path)
        if path.name == "x.md":
            with path.open("a", encoding="utf-8") as fh:
                fh.write("!")
            future = path.stat().st_mtime + 60
            os.utime(path, (future, future))
        return digest

    doc_scan._sha256_file = _hash_then_rewrite
    try:
        pass_n = doc_scan.scan(root, cache, roots=("sample/draft",))
    finally:
        doc_scan._sha256_file = original_hash

    assert "sample/draft/x.md" in pass_n.skipped, pass_n.skipped

    time.sleep(0.02)
    (root / "sample/draft/x.md").write_text("hello", encoding="utf-8")  # resaved AFTER the cache write
    (root / "sample/draft/a.md").unlink()

    result = doc_scan.scan(root, cache, roots=("sample/draft",))
    events, new_absent = doc_scan.availability_events(state, set(), result)
    assert events == [{"type": "unavailable", "artifact_id": "a1"}], events
    assert new_absent == {"a1"}


def _st_malformed_cache_entry_rehashed(tmp: Path) -> None:
    """A non-list cache entry (a hand-edited or corrupted cache) must never
    crash the pass; it is treated as absent and the file is re-hashed."""
    root = tmp / "root"
    (root / "sample/draft").mkdir(parents=True)
    (root / "sample/draft/a.md").write_text("hello", encoding="utf-8")
    cache = tmp / "cache.json"
    cache.write_text(
        json.dumps({"written_at": time.time_ns(), "files": {"sample/draft/a.md": 5}}),
        encoding="utf-8")

    result = doc_scan.scan(root, cache, roots=("sample/draft",), write_cache=False)
    assert result.complete is True, "a malformed cache entry must not make the pass incomplete"
    assert result.files["sample/draft/a.md"] == hashlib.sha256(b"hello").hexdigest()


# ============================================================
# Self-test scenarios (Task 5: memory plan and guarded deletion)
# ============================================================


def _hs():
    """`scripts/hindsight-sync.py`, loaded by path: a hyphenated name is not importable."""
    import importlib.util
    from scripts.utils.paths import get_workspace_root
    if "_doc_versions_hindsight_sync" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "_doc_versions_hindsight_sync", get_workspace_root() / "scripts" / "hindsight-sync.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules["_doc_versions_hindsight_sync"] = module
    return sys.modules["_doc_versions_hindsight_sync"]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _decided_family(*members: dict, actual: str) -> list[dict]:
    """Events: one family holding `members`, confirmed, with `actual` decided."""
    sha = next(m["sha256"] for m in members if m["artifact_id"] == actual)
    return [_ev("family_proposed", family_id="f1", source="created", members=list(members)),
            _ev("family_confirmed", family_id="f1"),
            _ev("decided", family_id="f1", artifact_id=actual, sha256=sha)]


def _upload(path: str, sha: str, parts: int = 1) -> dict:
    rec = {"path": path, "sha256": sha, "operation_id": "op-x", "sent": "2026-01-01T00:00:00+00:00"}
    if parts > 1:
        rec["operation_ids"] = [f"op-{i}" for i in range(parts)]
    return rec


def _history_fixture():
    """A decided family: `d/old.md` is history, `d/new.md` actual; the ledger sent both."""
    old, new = _sha("old text"), _sha("new text")
    state = vr.replay(_decided_family({"artifact_id": "h1", "path": "d/old.md", "sha256": old},
                                      {"artifact_id": "a1", "path": "d/new.md", "sha256": new}, actual="a1"))
    led = memory_plan.index_ledger([_upload("d/old.md", old), _upload("d/new.md", new)])
    result = doc_scan.ScanResult(complete=True, files={"d/old.md": old, "d/new.md": new})
    return state, led, result, old, new


def _st_history_deleted_when_ledger_matches(tmp: Path) -> None:
    state, led, result, _old, _new = _history_fixture()
    p = memory_plan.plan(state, led, result, 100)
    assert p.delete == ["d/old.md"], p.delete
    assert "d/old.md" in p.skip and "d/new.md" in p.upload and p.blocked is None, p
    led2 = memory_plan.index_ledger([_upload("d/old.md", _sha("an earlier edit")), _upload("d/new.md", _new)])
    assert memory_plan.plan(state, led2, result, 100).delete == [], "a ledger revision unlike the registry's deletes nothing"


def _st_reused_history_path_not_deleted(tmp: Path) -> None:
    state, led, result, _old, _new = _history_fixture()
    result.files["d/old.md"] = _sha("an unrelated new document at the old path")
    p = memory_plan.plan(state, led, result, 100)
    assert p.delete == [], p.delete
    assert "d/old.md" not in p.upload and "d/old.md" not in p.skip, p


def _st_proposed_member_held(tmp: Path) -> None:
    new, prop = _sha("new text"), _sha("candidate text")
    events = _decided_family({"artifact_id": "a1", "path": "d/new.md", "sha256": new}, actual="a1")
    events.append(_ev("member_proposed", family_id="f1",
                      member={"artifact_id": "p1", "path": "d/candidate.md", "sha256": prop}))
    led = memory_plan.index_ledger([_upload("d/new.md", new), _upload("d/candidate.md", prop)])
    result = doc_scan.ScanResult(complete=True, files={"d/new.md": new, "d/candidate.md": prop})
    p = memory_plan.plan(vr.replay(events), led, result, 100)
    assert "d/candidate.md" in p.hold and "d/candidate.md" not in p.upload and p.delete == [], p
    assert "d/new.md" in p.upload, "memory keeps the previous actual"


def _st_partial_scan_plans_no_deletes(tmp: Path) -> None:
    state, led, result, _old, _new = _history_fixture()
    result.complete = False
    p = memory_plan.plan(state, led, result, 100)
    assert p.delete == [] and p.delete_path == {}, p.delete


def _st_guard_blocks_mass_delete(tmp: Path) -> None:
    members = [{"artifact_id": f"h{i}", "path": f"d/old{i}.md", "sha256": _sha(f"old {i}")} for i in range(21)]
    members.append({"artifact_id": "a1", "path": "d/new.md", "sha256": _sha("new")})
    state = vr.replay(_decided_family(*members, actual="a1"))
    led = memory_plan.index_ledger([_upload(m["path"], m["sha256"]) for m in members])
    result = doc_scan.ScanResult(complete=True, files={m["path"]: m["sha256"] for m in members})
    p = memory_plan.plan(state, led, result, 100_000)
    assert p.delete == [] and p.blocked and p.blocked.startswith("21 deletions exceed the guard"), p.blocked
    # the 1 % rule on its own: 3 deletions against 100 uploaded paths
    state3 = vr.replay(_decided_family(*members[:3], members[-1], actual="a1"))
    p3 = memory_plan.plan(state3, led, result, 100)
    assert p3.delete == [] and p3.blocked, p3
    p1 = memory_plan.plan(vr.replay(_decided_family(members[0], members[-1], actual="a1")), led, result, 100)
    assert p1.delete == ["d/old0.md"] and p1.blocked is None, "one deletion in a hundred is within the guard"


def _st_header_change_triggers_reupload(tmp: Path) -> None:
    hs = _hs()
    root = _tree(tmp, {"d/new.md": "new text with the limit set to 100", "d/other.md": "outside every family"})
    old, new = _sha("old text"), _sha("new text with the limit set to 100")
    events = _decided_family({"artifact_id": "h1", "path": "d/old.md", "sha256": old},
                             {"artifact_id": "a1", "path": "d/new.md", "sha256": new}, actual="a1")
    result = doc_scan.ScanResult(complete=True, files={"d/new.md": new})
    p1 = memory_plan.plan(vr.replay(events), {}, result, 0, root=root)
    header = p1.upload["d/new.md"]
    assert "Изменения: не описаны." in header and "d/old.md" in header and "limit" not in header, header
    seen = {q: {**hs._ledger_meta(root / q), "path": q} for q in ("d/new.md", "d/other.md")}
    seen["d/new.md"]["header_fp"] = p1.fingerprints["d/new.md"]
    assert not hs.needs_send(root, "d/new.md", seen, p1.fingerprints), "same fingerprint, same bytes: no resend"
    assert not hs.needs_send(root, "d/other.md", seen, p1.fingerprints), "a file outside families is unchanged"
    events.append(_ev("summary", family_id="f1", from_sha=old, to_sha=new, text="updated the section on limits"))
    p2 = memory_plan.plan(vr.replay(events), {}, result, 0, root=root)
    assert "Изменения: updated the section on limits." in p2.upload["d/new.md"], p2.upload
    assert p2.fingerprints["d/new.md"] != p1.fingerprints["d/new.md"]
    assert hs.needs_send(root, "d/new.md", seen, p2.fingerprints), "a new description must resend the actual"
    items = hs.items_for(root, "d/new.md", header=p2.upload["d/new.md"])
    assert items[0]["content"].startswith("Workspace file: d/new.md\n\nАктуальная версия от "), items[0]["content"][:80]
    small = hs.items_for(root, "d/new.md", max_bytes=len(p2.upload["d/new.md"].encode()) + 12,
                         header=p2.upload["d/new.md"])
    for it in small:
        body = it["content"].split("\n\n", 1)[1]
        assert len(body.encode("utf-8")) <= len(p2.upload["d/new.md"].encode()) + 12, (len(body.encode()), it)
    plain = hs.items_for(root, "d/other.md")
    assert plain[0]["content"] == "Workspace file: d/other.md\n\noutside every family", plain


def _twin_text(source_sha: str, body: str) -> str:
    return f"# Extract: old.pdf\n\n> Source SHA-256: {source_sha}; converter: invented 1.0\n\n{body}\n"


def _st_twin_ids_follow_original(tmp: Path) -> None:
    pdf_old, pdf_new = b"%PDF invented old", b"%PDF invented new"
    old, new = hashlib.sha256(pdf_old).hexdigest(), hashlib.sha256(pdf_new).hexdigest()
    root = tmp / "root"
    (root / "d").mkdir(parents=True)
    (root / "d/old.pdf").write_bytes(pdf_old)
    (root / "d/new.pdf").write_bytes(pdf_new)
    twin = _twin_text(old, "old body")
    (root / "d/old-extract.md").write_text(twin, encoding="utf-8")
    (root / "d/new-extract.md").write_text(_twin_text(new, "new body"), encoding="utf-8")
    state = vr.replay(_decided_family({"artifact_id": "h1", "path": "d/old.pdf", "sha256": old},
                                      {"artifact_id": "a1", "path": "d/new.pdf", "sha256": new}, actual="a1"))
    led = memory_plan.index_ledger([_upload("d/old-extract.md", _sha(twin), parts=2)])
    assert memory_plan.document_ids_for("d/old.pdf", led) == ["d/old-extract.md#part-01", "d/old-extract.md#part-02"]
    result = doc_scan.ScanResult(complete=True, files={"d/old.pdf": old, "d/new.pdf": new})
    p = memory_plan.plan(state, led, result, 1000, root=root)
    assert p.delete == ["d/old-extract.md#part-01", "d/old-extract.md#part-02"], p.delete
    assert "d/new-extract.md" in p.upload and "d/old-extract.md" in p.skip, p
    # a twin whose provenance names another original is not this member's to delete
    (root / "d/old-extract.md").write_text(_twin_text(_sha("another original"), "old body"), encoding="utf-8")
    led2 = memory_plan.index_ledger([_upload("d/old-extract.md", hashlib.sha256(
        (root / "d/old-extract.md").read_bytes()).hexdigest())])
    assert memory_plan.plan(state, led2, result, 1000, root=root).delete == []


def _st_delete_404_counts_as_done(tmp: Path) -> None:
    import urllib.error
    hs = _hs()
    calls: list[tuple[str, str]] = []

    def fake_api(method, url, key, body=None, code=404):
        calls.append((method, url))
        raise urllib.error.HTTPError(url, code, "invented", None, None)

    plan = memory_plan.Plan(delete=["d/old a.md"], delete_path={"d/old a.md": "d/old a.md"})
    saved = hs._api
    try:
        hs._api = fake_api
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = hs.run_deletions(plan, "http://invented.invalid", "bank", "key", 7, tmp / "reg.jsonl",
                                  read_state=lambda: vr.State(generation=7))
        lines = [json.loads(x) for x in out.getvalue().splitlines()]
        assert rc == 0, rc
        assert calls == [("DELETE", "http://invented.invalid/v1/default/banks/bank/documents/d%2Fold%20a.md")], calls
        assert lines[0] == {"path": "d/old a.md", "delete_intent": "d/old a.md", "generation": 7, "at": lines[0]["at"]}
        assert lines[1]["deleted"] == "d/old a.md" and lines[1]["absent"] is True, lines
        hs._api = lambda *a, **k: fake_api(*a, code=500, **k)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = hs.run_deletions(plan, "http://invented.invalid", "bank", "key", 7, tmp / "reg.jsonl",
                                  read_state=lambda: vr.State(generation=7))
        lines = [json.loads(x) for x in out.getvalue().splitlines()]
        assert rc == 1 and len(lines) == 2 and "delete_intent" in lines[0] and "delete_failed" in lines[1], lines
        led = memory_plan.index_ledger([_upload("d/old a.md", "s"), *lines])
        assert memory_plan.document_ids_for("d/old a.md", led) == ["d/old a.md"], "an unanswered intent is asked again"
    finally:
        hs._api = saved


def _st_corrupt_registry_blocks_deletions(tmp: Path) -> None:
    hs = _hs()
    reg = tmp / "reg.jsonl"
    good = json.dumps(_ev("family_proposed", family_id="f1", source="created", members=[_m("a")]))
    reg.write_text(good + "\n{not json\n" + good + "\n", encoding="utf-8")
    root = _tree(tmp, {"sample/draft/a.md": "a"})
    with contextlib.redirect_stderr(io.StringIO()):
        state, result, why = hs.prepare_memory(root, reg, tmp / "cache.json", False, roots=("sample/draft",))
    assert state is None and why and result.complete is False, (state, why)
    assert hs._could_be_member("sample/draft/a.md", ("sample/draft",))
    assert hs._could_be_member("sample/draft/b-extract.md", ("sample/draft",)), "a twin waits too"
    assert not hs._could_be_member("notes/c.md", ("sample/draft",)), "a file no family can hold is still sent"
    called = []
    saved = hs._api
    try:
        hs._api = lambda *a, **k: called.append(a)
        plan = memory_plan.Plan(delete=["x"], delete_path={"x": "x"})
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            rc = hs.deletion_phase(reg, 1, plan, lambda st: plan, "http://invented.invalid", "b", "k")
        assert rc == 0 and called == [], called
    finally:
        hs._api = saved


def _st_out_of_scope_member_never_deleted(tmp: Path) -> None:
    gone, new = _sha("gone"), _sha("new")
    events = _decided_family({"artifact_id": "g1", "path": "d/gone.md", "sha256": gone},
                             {"artifact_id": "a1", "path": "d/new.md", "sha256": new}, actual="a1")
    events += [_ev("unavailable", artifact_id="g1"), _ev("missing", artifact_id="g1")]
    state = vr.replay(events)
    led = memory_plan.index_ledger([_upload("d/gone.md", gone), _upload("d/new.md", new)])
    inside = doc_scan.ScanResult(complete=True, files={"d/new.md": new})
    assert memory_plan.plan(state, led, inside, 100).delete == ["d/gone.md"], "a missing member is mirrored"
    outside = doc_scan.ScanResult(complete=True, files={"d/new.md": new}, out_of_scope=["g1"])
    assert memory_plan.plan(state, led, outside, 100).delete == []
    unavailable = vr.replay(events[:-1])
    assert memory_plan.plan(unavailable, led, inside, 100).delete == [], "unavailable is not yet missing"


def _st_registry_file_never_uploaded(tmp: Path) -> None:
    import subprocess
    hs = _hs()
    root = _tree(tmp, {"notes/a.md": "a", "keep/reg.yaml": "x: 1", "keep/reg.yaml.torn": "t"})
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    saved = vr.registry_path
    try:
        vr.registry_path = lambda: root / "keep" / "reg.yaml"
        got = hs.candidates(root)
    finally:
        vr.registry_path = saved
    assert got == ["notes/a.md"], got


def _st_generation_change_replans_then_skips(tmp: Path) -> None:
    hs = _hs()
    reg = tmp / "reg.jsonl"
    calls: list[str] = []
    saved = hs._api
    try:
        hs._api = lambda method, url, key, body=None: calls.append(url) or {}
        plan = memory_plan.Plan(delete=["stale"], delete_path={"stale": "stale"})
        fresh = memory_plan.Plan(delete=["fresh"], delete_path={"fresh": "fresh"})
        replans: list[int] = []

        def replan(st):
            replans.append(st.generation)
            return fresh

        gens = iter([5, 6])
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            rc = hs.deletion_phase(reg, 4, plan, replan, "http://invented.invalid", "b", "k",
                                   read_state=lambda: vr.State(generation=next(gens)))
        assert rc == 0 and replans == [5] and calls == [], (replans, calls)
        gens = iter([5, 5, 5])
        out = io.StringIO()
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(out):
            rc = hs.deletion_phase(reg, 4, plan, replan, "http://invented.invalid", "b", "k",
                                   read_state=lambda: vr.State(generation=next(gens)))
        assert rc == 0 and len(calls) == 1 and calls[0].endswith("/documents/fresh"), calls
        assert json.loads(out.getvalue().splitlines()[0])["generation"] == 5
        gens = iter([4, 4])
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            hs.deletion_phase(reg, 4, plan, replan, "http://invented.invalid", "b", "k",
                              read_state=lambda: vr.State(generation=next(gens)))
        assert calls[-1].endswith("/documents/stale") and replans == [5, 5], "an unchanged generation keeps the plan"
    finally:
        hs._api = saved


def _fake_delete_api(calls: list, check=None):
    def fake(method, url, key, body=None):
        if check:
            check()
        calls.append(url)
        return {}
    return fake


def _st_deletes_off_by_default(tmp: Path) -> None:
    hs = _hs()
    reg = tmp / "reg.jsonl"
    assert hs.build_parser().parse_args(["push"]).allow_deletes is False
    plan = memory_plan.Plan(delete=["d/a.md"], delete_path={"d/a.md": "d/a.md"})
    calls: list = []
    saved = hs._api
    try:
        hs._api = _fake_delete_api(calls)
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            rc = hs.finish_deletions(plan, False, reg, 3, lambda st: plan, "http://invented.invalid", "b", "k",
                                     read_state=lambda: vr.State(generation=3))
        assert rc == 0 and calls == [] and out.getvalue() == "", (calls, out.getvalue())
        assert "delete d/a.md (disabled: --allow-deletes not given)" in err.getvalue(), err.getvalue()
        assert not reg.with_suffix(".lock").exists(), "no lock is taken without --allow-deletes"
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            hs.finish_deletions(memory_plan.Plan(), True, reg, 3, lambda st: plan, "http://invented.invalid",
                                "b", "k", read_state=lambda: vr.State(generation=3))
        assert calls == [] and not reg.with_suffix(".lock").exists(), "nothing to delete: no lock either"
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            rc = hs.finish_deletions(plan, True, reg, 3, lambda st: plan, "http://invented.invalid", "b", "k",
                                     read_state=lambda: vr.State(generation=3))
        assert rc == 0 and len(calls) == 1 and calls[0].endswith("/documents/d%2Fa.md"), calls
    finally:
        hs._api = saved


def _st_lock_not_held_during_delete(tmp: Path) -> None:
    import fcntl
    hs = _hs()
    reg = tmp / "reg.jsonl"
    acquired: list[bool] = []

    def probe():
        with open(reg.with_suffix(".lock"), "a") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                acquired.append(False)
                return
            acquired.append(True)
            fcntl.flock(fh, fcntl.LOCK_UN)

    calls: list = []
    saved = hs._api
    try:
        hs._api = _fake_delete_api(calls, probe)
        plan = memory_plan.Plan(delete=["a", "b"], delete_path={"a": "a", "b": "b"})
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            rc = hs.deletion_phase(reg, 2, plan, lambda st: plan, "http://invented.invalid", "b", "k",
                                   read_state=lambda: vr.State(generation=2))
        assert rc == 0 and acquired == [True, True], acquired
    finally:
        hs._api = saved


def _st_generation_bump_between_deletions_stops(tmp: Path) -> None:
    hs = _hs()
    calls: list = []
    saved = hs._api
    try:
        hs._api = _fake_delete_api(calls)
        plan = memory_plan.Plan(delete=["a", "b"], delete_path={"a": "a", "b": "b"})
        gens = iter([2, 2, 3])  # the phase re-check, before "a", before "b"
        out = io.StringIO()
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(out):
            rc = hs.deletion_phase(tmp / "reg.jsonl", 2, plan, lambda st: plan, "http://invented.invalid",
                                   "b", "k", read_state=lambda: vr.State(generation=next(gens)))
        lines = [json.loads(x) for x in out.getvalue().splitlines()]
        assert rc == 0 and len(calls) == 1 and calls[0].endswith("/documents/a"), calls
        assert [x.get("delete_intent") or x.get("deleted") for x in lines] == ["a", "a"], lines
    finally:
        hs._api = saved


def _st_other_transport_error_is_recorded(tmp: Path) -> None:
    hs = _hs()
    saved = hs._api
    try:
        def boom(method, url, key, body=None):
            raise ValueError("invented transport failure")
        hs._api = boom
        plan = memory_plan.Plan(delete=["a", "b"], delete_path={"a": "a", "b": "b"})
        out = io.StringIO()
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(out):
            rc = hs.run_deletions(plan, "http://invented.invalid", "b", "k", 1, tmp / "reg.jsonl",
                                  read_state=lambda: vr.State(generation=1))
        lines = [json.loads(x) for x in out.getvalue().splitlines()]
        assert rc == 1 and len(lines) == 2 and lines[1]["delete_failed"] == "a", lines
        assert "ValueError" in lines[1]["error"], lines[1]
    finally:
        hs._api = saved


def _st_missing_actual_holds_history(tmp: Path) -> None:
    old, new = _sha("old text"), _sha("new text")
    events = _decided_family({"artifact_id": "h1", "path": "d/old.md", "sha256": old},
                             {"artifact_id": "a1", "path": "d/new.md", "sha256": new}, actual="a1")
    led = memory_plan.index_ledger([_upload("d/old.md", old), _upload("d/new.md", new)])
    result = doc_scan.ScanResult(complete=True, files={"d/old.md": old})
    unavailable = vr.replay(events + [_ev("unavailable", artifact_id="a1")])
    p = memory_plan.plan(unavailable, led, result, 1000)
    assert p.delete == [] and "d/old.md" in p.hold and any("unavailable" in n for n in p.notes), p
    missing = vr.replay(events + [_ev("unavailable", artifact_id="a1"), _ev("missing", artifact_id="a1")])
    p = memory_plan.plan(missing, led, result, 1000)
    assert p.delete == ["d/new.md"], "the missing actual's own documents go (M003), its history does not"
    assert "d/old.md" in p.hold and "d/old.md" not in p.upload and any("missing" in n for n in p.notes), p


def _st_reused_history_path_new_content_uploaded(tmp: Path) -> None:
    hs = _hs()
    root = _tree(tmp, {"d/old.md": "an unrelated new document", "d/new.md": "new text"})
    old, new = _sha("old text"), _sha("new text")
    state = vr.replay(_decided_family({"artifact_id": "h1", "path": "d/old.md", "sha256": old},
                                      {"artifact_id": "a1", "path": "d/new.md", "sha256": new}, actual="a1"))
    led = memory_plan.index_ledger([_upload("d/old.md", old), _upload("d/new.md", new)])
    result = doc_scan.ScanResult(complete=True, files={"d/old.md": _sha("an unrelated new document"), "d/new.md": new})
    p = memory_plan.plan(state, led, result, 1000, root=root)
    assert p.delete == [] and "d/old.md" not in p.hold | p.skip and "d/old.md" not in p.upload, p
    seen = {"d/old.md": {"path": "d/old.md", "sha256": old, "bytes": 1, "mtime": 1}}
    assert hs.needs_send(root, "d/old.md", seen, p.fingerprints), "new content at the old path is sent"
    assert hs.items_for(root, "d/old.md", header=p.upload.get("d/old.md", ""))[0]["content"] == \
        "Workspace file: d/old.md\n\nan unrelated new document", "as an ordinary file, without a header"


def _st_restore_requeues(tmp: Path) -> None:
    """`restore` appends one ledger line, in `status --requeue-failed`'s shape."""
    root = _tree(tmp, {"d/a.md": "hello"})
    ledger = tmp / "ledger.jsonl"
    rc = main(["restore", "d/a.md", "--root", str(root), "--ledger", str(ledger)])
    lines = [ln for ln in ledger.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert rc == 0 and len(lines) == 1, (rc, lines)
    rec = json.loads(lines[0])
    assert rec["path"] == "d/a.md" and rec["requeue"] is True, rec
    assert "reason" in rec and "sent" in rec, rec

    # a second restore appends a second line; nothing already there is touched
    rc2 = main(["restore", "d/a.md", "--root", str(root), "--ledger", str(ledger)])
    lines2 = [ln for ln in ledger.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert rc2 == 0 and len(lines2) == 2 and lines2[0] == lines[0], lines2


def _st_restore_refuses_bad_path(tmp: Path) -> None:
    """An absolute path, a `..` escape, and a path missing on disk are all refused,
    with rc 2 and nothing written to the ledger."""
    root = _tree(tmp, {"d/a.md": "hello"})
    ledger = tmp / "ledger.jsonl"
    for bad in ("/etc/passwd", "../escape.md", "d/missing.md"):
        rc = main(["restore", bad, "--root", str(root), "--ledger", str(ledger)])
        assert rc == 2, (bad, rc)
    assert not ledger.exists(), "a refused restore must write nothing"


def _st_restore_writes_canonical_path(tmp: Path) -> None:
    """`restore` ledgers the canonical root-relative path of the real file, never the
    one typed: `record` already does this (`_st_record_stores_canonical_relative_path`),
    and `restore` must agree so the same path never reaches the ledger two ways."""
    root = _tree(tmp, {"d/a.md": "hello", "d/x/b.md": "sibling"})
    ledger = tmp / "ledger.jsonl"
    rc = main(["restore", "d/x/../a.md", "--root", str(root), "--ledger", str(ledger)])
    lines = [ln for ln in ledger.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert rc == 0 and len(lines) == 1, (rc, lines)
    rec = json.loads(lines[0])
    assert rec["path"] == "d/a.md", rec


def _st_restore_ledger_matches_hindsight_sync(tmp: Path) -> None:
    """`_default_ledger()` must resolve to the exact path `hindsight-sync.py`'s
    own `_ledger()` resolves, so the two cannot silently drift apart. Pins the
    duplicate the module docstring explains, rather than trusting it by eye."""
    hs = _hs()
    assert _default_ledger() == hs._ledger(), (_default_ledger(), hs._ledger())


def _st_pending_count_counts_families(tmp: Path) -> None:
    """`pending --count` counts an unconfirmed family and a confirmed family still
    awaiting a decision, and excludes a confirmed family already fully decided."""
    reg = tmp / "reg.jsonl"
    vr.append({"type": "family_proposed", "family_id": "f1", "source": "created",
               "members": [_m("a1"), _m("a2")]}, path=reg)  # unconfirmed: counts
    vr.append({"type": "family_proposed", "family_id": "f2", "source": "created",
               "members": [_m("b1"), _m("b2")]}, path=reg)
    vr.append({"type": "family_confirmed", "family_id": "f2"}, path=reg)
    vr.append({"type": "decided", "family_id": "f2", "artifact_id": "b1",
               "sha256": _m("b1")["sha256"]}, path=reg)  # confirmed and decided: does not count
    vr.append({"type": "family_proposed", "family_id": "f3", "source": "created",
               "members": [_m("c1"), _m("c2")]}, path=reg)
    vr.append({"type": "family_confirmed", "family_id": "f3"}, path=reg)  # confirmed, no decision: counts
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(["pending", "--count", "--registry", str(reg)])
    out = buf.getvalue().strip()
    assert rc == 0 and out == "2", (rc, out)


def _st_pending_count_absent_registry_is_zero(tmp: Path) -> None:
    """A registry that has never been written answers `0`, not an error: this is
    the case the `/prime` check relies on to stay silent on a fresh clone."""
    reg = tmp / "does-not-exist.jsonl"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(["pending", "--count", "--registry", str(reg)])
    assert rc == 0 and buf.getvalue().strip() == "0", (rc, buf.getvalue())


def _st_pending_count_corrupt_registry_exits_nonzero(tmp: Path) -> None:
    """A corrupt registry must not answer a plausible-looking count: `pending`
    propagates `RegistryCorrupt` rather than reading around the damage, which is
    what lets the `/prime` check tell "needs attention" apart from "quiet"."""
    reg = tmp / "reg.jsonl"
    reg.write_text('{"broken\n' + json.dumps({"schema_version": 1, "event_id": "e", "seq": 2,
                   "type": "family_confirmed", "family_id": "f1"}) + "\n", encoding="utf-8")
    try:
        main(["pending", "--count", "--registry", str(reg)])
    except vr.RegistryCorrupt:
        return
    raise AssertionError("a corrupt registry must not silently answer 'pending --count'")


# ============================================================
# Task 8 self-tests: the detector proposes, never decides
# ============================================================

_VOCAB = [f"w{i}" for i in range(400)]


def _prose(seed: int, n: int) -> list[str]:
    """`n` invented words, deterministic for `seed`, from a 400-word vocabulary."""
    rng = random.Random(seed)  # noqa: S311 - deterministic fixture text, not cryptographic
    return [rng.choice(_VOCAB) for _ in range(n)]


def _edited(words: list[str], seed: int, k: int) -> list[str]:
    """`words` with `k` positions replaced by invented words not in the vocabulary."""
    rng = random.Random(seed)  # noqa: S311 - deterministic fixture text, not cryptographic
    out = list(words)
    for i in rng.sample(range(len(out)), k):
        out[i] = f"edit{seed}x{i}"
    return out


def _detect(root: Path, tmp: Path, state: vr.State | None = None):
    """Scan `sample/draft` under `root` without writing a cache, then detect."""
    result = doc_scan.scan(root, tmp / "scan-cache.json", roots=("sample/draft",), write_cache=False)
    assert result.complete, result.errors
    return result, doc_detector.detect(state or vr.State(), result, root)


def _proposed_path_sets(events: list[dict]) -> list[set[str]]:
    return [{m["path"] for m in ev["members"]} for ev in events if ev["type"] == "family_proposed"]


def _plan_tree(tmp: Path, extra: dict[str, str] | None = None) -> Path:
    base = _prose(1, 300)
    files = {
        "sample/draft/plan-v1.md": " ".join(base),
        "sample/draft/plan-v2.md": " ".join(_edited(base, 2, 8)),
        "sample/draft/unrelated.md": " ".join(_prose(99, 300)),
    }
    files.update(extra or {})
    return _tree(tmp, files)


def _st_v1_v2_proposed(tmp: Path) -> None:
    root = _plan_tree(tmp)
    _, det = _detect(root, tmp)
    assert _proposed_path_sets(det.events) == [
        {"sample/draft/plan-v1.md", "sample/draft/plan-v2.md"}], det.events
    ev = det.events[0]
    assert ev["source"] == "detector" and ev["reasons"], ev
    assert "name match" in ev["reasons"][0] and "text " in ev["reasons"][0], ev["reasons"]


def _st_monthly_reports_not_proposed(tmp: Path) -> None:
    """Same template, a different month in each name: not versions.

    The honest half proves the date signal is what refuses: the very same
    texts under names carrying no period, only revision markers, ARE
    proposed, so the template similarity alone clears the threshold.
    """
    template = _prose(5, 400)
    texts = {m: " ".join(_edited(template, 10 + m, 6)) for m in (1, 2, 3)}
    dated = _tree(tmp / "dated", {f"sample/draft/report-2026-0{m}.md": t for m, t in texts.items()})
    _, det = _detect(dated, tmp / "dated")
    assert _proposed_path_sets(det.events) == [], det.events
    assert len(det.near_misses) == 3, ("each refused dated pair is a near miss", det.near_misses)
    plain = _tree(tmp / "plain", {f"sample/draft/report-v{m}.md": t for m, t in texts.items()})
    _, det2 = _detect(plain, tmp / "plain")
    assert len(_proposed_path_sets(det2.events)) == 1, det2.events


def _st_rejected_pair_not_reproposed_after_rename(tmp: Path) -> None:
    root = _plan_tree(tmp)
    reg = tmp / "reg.jsonl"
    rc = main(["reject", "sample/draft/plan-v1.md", "sample/draft/plan-v2.md",
               "--root", str(root), "--registry", str(reg)])
    assert rc == 0
    (root / "sample/draft/plan-v1.md").rename(root / "sample/draft/budget-v1.md")
    (root / "sample/draft/plan-v2.md").rename(root / "sample/draft/budget-v2.md")
    state = vr.replay(vr.read_events(reg)[0])
    _, det = _detect(root, tmp, state)
    assert _proposed_path_sets(det.events) == [], det.events
    _, honest = _detect(root, tmp)
    assert _proposed_path_sets(honest.events) == [
        {"sample/draft/budget-v1.md", "sample/draft/budget-v2.md"}], honest.events


def _scan_cli(root: Path, tmp: Path, *extra: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = main(["scan", *extra, "--root", str(root), "--registry", str(tmp / "reg.jsonl"),
                   "--scan-cache", str(tmp / "scan-cache.json"),
                   "--detector-cache", str(tmp / "detector-cache.json"),
                   "--scope-root", "sample/draft"])
    return rc, out.getvalue()


def _st_rerun_is_idempotent(tmp: Path) -> None:
    root = _plan_tree(tmp)
    rc1, out1 = _scan_cli(root, tmp)
    rc2, out2 = _scan_cli(root, tmp)
    assert rc1 == 0 and rc2 == 0, (out1, out2)
    events = vr.read_events(tmp / "reg.jsonl")[0]
    assert [e["type"] for e in events] == ["family_proposed"], events
    assert "proposed families: 0" in out2, out2
    # Idempotent by the proposal's content, not only its id: a family recorded
    # by hand (a random id), its files then renamed, is still not re-proposed.
    other = _plan_tree(tmp / "other")
    reg = tmp / "other" / "reg.jsonl"
    assert main(["record", "sample/draft/plan-v1.md", "sample/draft/plan-v2.md",
                 "--root", str(other), "--registry", str(reg)]) == 0
    (other / "sample/draft/plan-v1.md").rename(other / "sample/draft/sheet-v1.md")
    (other / "sample/draft/plan-v2.md").rename(other / "sample/draft/sheet-v2.md")
    _, det = _detect(other, tmp / "other", vr.replay(vr.read_events(reg)[0]))
    assert det.events == [], det.events


def _st_identical_bytes_reported_as_duplicates(tmp: Path) -> None:
    text = " ".join(_prose(3, 300))
    root = _tree(tmp, {"sample/draft/plan-v1.md": text, "sample/draft/plan-v1 copy.md": text})
    _, det = _detect(root, tmp)
    assert det.events == [], det.events
    assert det.duplicates == [["sample/draft/plan-v1 copy.md", "sample/draft/plan-v1.md"]], det.duplicates
    words = text.split()
    (root / "sample/draft/plan-v2.md").write_text(" ".join(_edited(words, 2, 8)), encoding="utf-8")
    _, det = _detect(root, tmp)
    assert _proposed_path_sets(det.events) == [
        {"sample/draft/plan-v1.md", "sample/draft/plan-v2.md"}], ("the copy is not the representative", det.events)


def _st_representation_not_proposed(tmp: Path) -> None:
    root = _tree(tmp, {"sample/draft/deck.docx": "docx bytes", "sample/draft/deck.pdf": "pdf bytes"})
    _, det = _detect(root, tmp)
    assert det.events == [], det.events
    assert det.representations == [["sample/draft/deck.docx", "sample/draft/deck.pdf"]], det.representations


def _st_rejection_not_bypassed_by_third_file(tmp: Path) -> None:
    base = _prose(1, 300)
    root = _plan_tree(tmp, {"sample/draft/plan-v3.md": " ".join(_edited(base, 3, 2))})
    reg = tmp / "reg.jsonl"
    assert main(["reject", "sample/draft/plan-v1.md", "sample/draft/plan-v2.md",
                 "--root", str(root), "--registry", str(reg)]) == 0
    state = vr.replay(vr.read_events(reg)[0])
    _, det = _detect(root, tmp, state)
    for paths in _proposed_path_sets(det.events):
        assert not {"sample/draft/plan-v1.md", "sample/draft/plan-v2.md"} <= paths, det.events
    _, honest = _detect(root, tmp)
    assert _proposed_path_sets(honest.events) == [
        {"sample/draft/plan-v1.md", "sample/draft/plan-v2.md", "sample/draft/plan-v3.md"}], honest.events
    # Nor by naming a confirmed member: v1 is confirmed in {v1, v2}, so bundling it into a
    # `reject` call with v3 and an unrelated file must now refuse rather than quietly keying
    # a rejection by content while leaving the confirmed family dead to the detector (see
    # `_st_reject_paths_refuses_confirmed_members`), even though the pair inside the larger
    # set is still "not versions".
    reg2 = tmp / "reg2.jsonl"
    assert main(["record", "sample/draft/plan-v1.md", "sample/draft/plan-v2.md",
                 "--root", str(root), "--registry", str(reg2)]) == 0
    fid = next(iter(vr.replay(vr.read_events(reg2)[0]).families))
    assert main(["confirm", fid, "--root", str(root), "--registry", str(reg2)]) == 0
    _, joins = _detect(root, tmp, vr.replay(vr.read_events(reg2)[0]))
    assert [e["type"] for e in joins.events] == ["member_proposed"], joins.events
    before = reg2.read_bytes()
    rc = main(["reject", "sample/draft/plan-v1.md", "sample/draft/plan-v3.md", "sample/draft/unrelated.md",
               "--root", str(root), "--registry", str(reg2)])
    assert rc == 2 and reg2.read_bytes() == before, rc


def _st_new_file_joins_confirmed_family_as_member_proposed(tmp: Path) -> None:
    base = _prose(1, 300)
    root = _plan_tree(tmp)
    reg = tmp / "reg.jsonl"
    assert main(["record", "sample/draft/plan-v1.md", "sample/draft/plan-v2.md",
                 "--root", str(root), "--registry", str(reg)]) == 0
    fid = next(iter(vr.replay(vr.read_events(reg)[0]).families))
    (root / "sample/draft/plan-v3.md").write_text(" ".join(_edited(base, 3, 8)), encoding="utf-8")
    _, waiting = _detect(root, tmp, vr.replay(vr.read_events(reg)[0]))
    assert waiting.events == [], ("a still-proposed family waits for its decision", waiting.events)
    assert main(["confirm", fid, "--root", str(root), "--registry", str(reg)]) == 0
    state = vr.replay(vr.read_events(reg)[0])
    _, det = _detect(root, tmp, state)
    assert [e["type"] for e in det.events] == ["member_proposed"], det.events
    ev = det.events[0]
    assert ev["family_id"] == fid and ev["member"]["path"] == "sample/draft/plan-v3.md", ev
    for e in det.events:
        vr.append(e, path=reg)
    _, again = _detect(root, tmp, vr.replay(vr.read_events(reg)[0]))
    assert again.events == [], again.events


def _st_possible_move_proposed_to_family(tmp: Path) -> None:
    """A copy of an unavailable member's bytes that the scan did not relocate."""
    root = _plan_tree(tmp)
    reg = tmp / "reg.jsonl"
    assert main(["record", "sample/draft/plan-v1.md", "sample/draft/plan-v2.md",
                 "--root", str(root), "--registry", str(reg)]) == 0
    st = vr.replay(vr.read_events(reg)[0])
    fid = next(iter(st.families))
    assert main(["confirm", fid, "--root", str(root), "--registry", str(reg)]) == 0
    aid = next(m.artifact_id for m in vr.replay(vr.read_events(reg)[0]).families[fid].members.values()
               if m.path == "sample/draft/plan-v1.md")
    (root / "sample/draft/plan-v1.md").rename(root / "sample/draft/moved.md")
    vr.append({"type": "unavailable", "artifact_id": aid}, path=reg)
    _, det = _detect(root, tmp, vr.replay(vr.read_events(reg)[0]))
    assert [e["type"] for e in det.events] == ["member_proposed"], det.events
    ev = det.events[0]
    assert ev["member"]["path"] == "sample/draft/moved.md" and ev["family_id"] == fid, ev
    assert any("possible move" in r for r in ev["reasons"]), ev
    vr.append(ev, path=reg)
    _, again = _detect(root, tmp, vr.replay(vr.read_events(reg)[0]))
    assert again.events == [], again.events


def _st_shared_template_weighs_little(tmp: Path) -> None:
    """Eight different documents on one long template are not versions of each other.

    Their names match (only a revision marker differs), so the text alone
    decides. Counted without weights, any two of them share more than half
    their shingles; weighted by rarity, the template counts for little and
    the own text decides.
    """
    template = _prose(7, 260)
    files = {f"sample/draft/memo-v{i}.md": " ".join(template + _prose(100 + i, 100)) for i in range(1, 9)}
    root = _tree(tmp, files)
    _, det = _detect(root, tmp)
    assert _proposed_path_sets(det.events) == [], det.events
    a = set(doc_detector.shingles(files["sample/draft/memo-v1.md"]))
    b = set(doc_detector.shingles(files["sample/draft/memo-v2.md"]))
    assert len(a & b) / len(a | b) > doc_detector.SAME_NAME_THRESHOLD, "fixture must clear the bar when every shingle counts the same"


def _st_no_text_falls_back_to_name(tmp: Path) -> None:
    root = _tree(tmp, {"sample/draft/deck-v1.docx": "first bytes", "sample/draft/deck-v2.docx": "second bytes",
                       "sample/draft/other.docx": "third bytes"})
    _, det = _detect(root, tmp)
    assert _proposed_path_sets(det.events) == [
        {"sample/draft/deck-v1.docx", "sample/draft/deck-v2.docx"}], det.events
    assert "comparison unavailable" in det.events[0]["reasons"][0], det.events[0]["reasons"]


def _window_tree(tmp: Path, names: list[str], *, shift: int = 60) -> Path:
    """Overlapping 300-word windows of one invented text, `shift` words apart, plus an unrelated file."""
    rng = random.Random(11)  # noqa: S311 - deterministic fixture text, not cryptographic
    words = [rng.choice(_VOCAB) for _ in range(300 + shift * len(names))]
    files = {f"sample/draft/{n}": " ".join(words[i * shift:i * shift + 300]) for i, n in enumerate(names)}
    files["sample/draft/unrelated.md"] = " ".join(_prose(99, 300))
    return _tree(tmp, files)


def _st_empty_generic_names_in_siblings_not_chained(tmp: Path) -> None:
    """Same generic name, no usable text, sibling directories: no link on the name alone."""
    root = _tree(tmp, {"sample/draft/a/markitdown.md": "!!!", "sample/draft/b/markitdown.md": "???",
                       "sample/draft/c/markitdown.md": ";;;"})
    _, det = _detect(root, tmp)
    assert det.events == [], det.events


def _st_sparse_group_refused_dense_kept(tmp: Path) -> None:
    """A chain linked only neighbour to neighbour (4 of 10 pairs) is refused; 9 of 10 is kept."""
    sparse = _window_tree(tmp / "sparse", [f"note-v{i}.md" for i in range(1, 6)], shift=100)
    _, det = _detect(sparse, tmp / "sparse")
    assert det.events == [], det.events
    assert any("density" in n for n in det.notes), det.notes
    base = _prose(1, 300)
    names = ["plan-v1.md", "plan-v2.md", "plan-v3.md", "plan-v4 2026-01.md", "plan-v5 2026-02.md"]
    dense = _tree(tmp / "dense", {f"sample/draft/{n}": " ".join(_edited(base, 20 + i, 3))
                                  for i, n in enumerate(names)})
    _, det = _detect(dense, tmp / "dense")
    assert [len(p) for p in _proposed_path_sets(det.events)] == [5], det.events
    assert len(det.events[0]["reasons"]) == 9, det.events[0]["reasons"]


def _st_rejected_family_does_not_freeze_its_files(tmp: Path) -> None:
    """A rejected family no longer holds its files: v3 linked to v2 still forms {v2, v3}."""
    root = _window_tree(tmp, ["plan-v1.md", "plan-v2.md", "plan-v3.md"])
    v3 = (root / "sample/draft/plan-v3.md").read_text(encoding="utf-8")
    (root / "sample/draft/plan-v3.md").unlink()
    reg = tmp / "reg.jsonl"
    assert main(["record", "sample/draft/plan-v1.md", "sample/draft/plan-v2.md",
                 "--root", str(root), "--registry", str(reg)]) == 0
    fid = next(iter(vr.replay(vr.read_events(reg)[0]).families))
    assert main(["reject", fid, "--root", str(root), "--registry", str(reg)]) == 0
    (root / "sample/draft/plan-v3.md").write_text(v3, encoding="utf-8")
    _, det = _detect(root, tmp, vr.replay(vr.read_events(reg)[0]))
    assert _proposed_path_sets(det.events) == [
        {"sample/draft/plan-v2.md", "sample/draft/plan-v3.md"}], det.events
    # A rejected family shrunk to one revision still blocks no overlap.
    base = _prose(1, 300)
    other = _plan_tree(tmp / "shrunk", {"sample/draft/plan-v3.md": " ".join(_edited(base, 3, 8))})
    reg2 = tmp / "shrunk" / "reg.jsonl"
    assert main(["record", "sample/draft/plan-v1.md", "sample/draft/plan-v2.md",
                 "--root", str(other), "--registry", str(reg2)]) == 0
    st = vr.replay(vr.read_events(reg2)[0])
    fid = next(iter(st.families))
    assert main(["reject", fid, "--root", str(other), "--registry", str(reg2)]) == 0
    v2_aid = next(m.artifact_id for m in st.families[fid].proposed.values() if m.path.endswith("plan-v2.md"))
    vr.append({"type": "member_removed", "family_id": fid, "artifact_id": v2_aid}, path=reg2)
    (other / "sample/draft/plan-v2.md").unlink()
    _, det = _detect(other, tmp / "shrunk", vr.replay(vr.read_events(reg2)[0]))
    assert _proposed_path_sets(det.events) == [
        {"sample/draft/plan-v1.md", "sample/draft/plan-v3.md"}], det.events


def _st_duplicate_family_id_refused(tmp: Path) -> None:
    reg = tmp / "reg.jsonl"
    ev = {"type": "family_proposed", "family_id": "f1", "source": "created",
          "members": [{"artifact_id": "a1", "path": "d/x.md", "sha256": "1" * 64}]}
    vr.append(ev, path=reg)
    try:
        vr.append(ev, path=reg)
    except vr.RegistryCorrupt:
        pass
    else:
        raise AssertionError("a second family_proposed for one family_id must be refused")
    assert len(vr.read_events(reg)[0]) == 1
    try:
        vr.replay([_ev("family_proposed", family_id="f2", source="created", members=[_m("a1")]),
                   _ev("family_proposed", family_id="f2", source="created", members=[_m("a2")])])
    except vr.RegistryCorrupt:
        return
    raise AssertionError("replay must refuse a family proposed twice")


def _st_vanished_file_noted_not_crash(tmp: Path) -> None:
    """A file gone between the scan and the proposal is a note, never a crash."""
    root = _plan_tree(tmp)
    reg = tmp / "reg.jsonl"
    assert main(["record", "sample/draft/plan-v1.md", "sample/draft/plan-v2.md",
                 "--root", str(root), "--registry", str(reg)]) == 0
    st = vr.replay(vr.read_events(reg)[0])
    fid = next(iter(st.families))
    aid = next(m.artifact_id for m in st.families[fid].proposed.values() if m.path == "sample/draft/plan-v1.md")
    (root / "sample/draft/plan-v1.md").rename(root / "sample/draft/moved.md")
    vr.append({"type": "unavailable", "artifact_id": aid}, path=reg)
    result = doc_scan.scan(root, tmp / "scan-cache.json", roots=("sample/draft",), write_cache=False)
    (root / "sample/draft/moved.md").unlink()
    det = doc_detector.detect(vr.replay(vr.read_events(reg)[0]), result, root)
    assert det.events == [], det.events
    assert any("moved.md" in n for n in det.notes), det.notes


def _st_scan_batch_refused_whole(tmp: Path) -> None:
    """One bad event in the batch: nothing is appended, exit 2."""
    root = _plan_tree(tmp)
    dd = doc_detector
    real = dd.detect

    def broken(*a, **kw):
        det = real(*a, **kw)
        det.events.append({"type": "member_proposed", "family_id": "no-such-family", "source": "detector",
                           "member": {"artifact_id": "x", "path": "sample/draft/x.md", "sha256": "2" * 64},
                           "reasons": []})
        return det
    dd.detect = broken
    try:
        rc, out = _scan_cli(root, tmp)
    finally:
        dd.detect = real
    assert rc == 2, (rc, out)
    assert vr.read_events(tmp / "reg.jsonl")[0] == [], "nothing partial may be written"


def _st_office_owner_files_out_of_scope(tmp: Path) -> None:
    roots = ("sample/draft",)
    assert not doc_scope.in_scope("sample/draft/~$plan.docx", roots=roots)
    assert doc_scope.in_scope("sample/draft/plan.docx", roots=roots)


def _st_month_word_alone_is_not_a_period(tmp: Path) -> None:
    periods = doc_detector.periods
    assert periods("d/may-plan.md") == frozenset(), periods("d/may-plan.md")
    assert periods("d/marketing-march-notes.md") == frozenset()
    assert "m05" in periods("d/report-may-2026.md")
    assert "m03" in periods("d/notes-15-mar.md")
    assert periods("d/план-март.md") == frozenset()
    assert "m03" in periods("d/отчёт-март-2026.md")


def _st_day_stamps_are_save_dates_periods_are_not(tmp: Path) -> None:
    """Two day dates are two saves of one document; two months are two reports.

    The same pair of texts is proposed under day-stamped names (with `r02`
    and a separator difference folded away) and refused under month names.
    """
    base = _prose(31, 300)
    v1, v2 = " ".join(_edited(base, 41, 6)), " ".join(_edited(base, 42, 6))
    other = " ".join(_prose(99, 300))
    stamped = _tree(tmp / "stamped", {"sample/draft/20240105 price list.md": v1,
                                      "sample/draft/20240212 pricelist-r02.md": v2,
                                      "sample/draft/unrelated.md": other})
    _, det = _detect(stamped, tmp / "stamped")
    assert _proposed_path_sets(det.events) == [
        {"sample/draft/20240105 price list.md", "sample/draft/20240212 pricelist-r02.md"}], det.events
    assert "saved on different days" in det.events[0]["reasons"][0], det.events[0]["reasons"]
    score = float(det.events[0]["reasons"][0].split("; text ")[1].split()[0])
    assert doc_detector.SAME_NAME_THRESHOLD <= score < doc_detector.DATE_CONFLICT_THRESHOLD, score
    periodic = _tree(tmp / "periodic", {"sample/draft/price list 2024-01.md": v1,
                                        "sample/draft/price list 2024-02.md": v2,
                                        "sample/draft/unrelated.md": other})
    _, det2 = _detect(periodic, tmp / "periodic")
    assert _proposed_path_sets(det2.events) == [], det2.events
    assert len(det2.near_misses) == 1 and "date conflict" in det2.near_misses[0][3], det2.near_misses


def _st_alternatives_and_sources_never_versions(tmp: Path) -> None:
    """Nearly the same text under names that mark an alternative, a source, or a sibling: refused.

    The honest case: the same text under a name that only ADDS a plain
    remark is proposed, at the higher bar for differing names.
    """
    base = _prose(51, 300)
    near = " ".join(_edited(base, 52, 1))
    other = " ".join(_prose(99, 300))
    refused = [("offer.md", "offer with costs.md", "alternative (with)"),
               ("offer.md", "offer 5yr.md", "alternative (5yr)"),
               ("offer.md", "offer - 1,5M.md", "different amounts"),
               ("page.md", "page.src.md", "source and build"),
               ("step-0.md", "step-1.md", "beyond one holding the other")]
    for i, (a, b, why) in enumerate(refused):
        root = _tree(tmp / str(i), {f"sample/draft/{a}": " ".join(base), f"sample/draft/{b}": near,
                                    "sample/draft/unrelated.md": other})
        _, det = _detect(root, tmp / str(i))
        assert det.events == [], (a, b, det.events)
        assert any(why in nm[3] for nm in det.near_misses), (a, b, det.near_misses)
        if "source" in why:
            assert [f"sample/draft/{a}", f"sample/draft/{b}"] in det.representations, det.representations
    root = _tree(tmp / "honest", {"sample/draft/offer.md": " ".join(base),
                                  "sample/draft/offer notes added.md": near,
                                  "sample/draft/unrelated.md": other})
    _, det = _detect(root, tmp / "honest")
    assert _proposed_path_sets(det.events) == [
        {"sample/draft/offer.md", "sample/draft/offer notes added.md"}], det.events
    assert f"(threshold {doc_detector.CONTAINED_NAME_THRESHOLD:.2f})" in det.events[0]["reasons"][0], det.events


def _st_revision_line_across_folders(tmp: Path) -> None:
    """One document's revisions filed in three folders whose names match: one family.

    Generic file names in folders that name different documents, or name
    only a kind of content, are never paired, however close their text.
    """
    base = _prose(61, 300)
    line = {"sample/draft/tools/2024-01-10_tool-alpha/main.md": " ".join(_edited(base, 62, 4)),
            "sample/draft/tools/2024-02-01_tool-alpha-v2/main.md": " ".join(_edited(base, 63, 4)),
            "sample/draft/far/away/alpha-tool-v3/main.md": " ".join(_edited(base, 64, 4)),
            "sample/draft/unrelated.md": " ".join(_prose(99, 300))}
    root = _tree(tmp / "line", line)
    _, det = _detect(root, tmp / "line")
    assert _proposed_path_sets(det.events) == [set(line) - {"sample/draft/unrelated.md"}], det.events
    generic = {"sample/draft/runs/case-one/new.md": " ".join(_edited(base, 65, 4)),
               "sample/draft/runs/case-two/new.md": " ".join(_edited(base, 66, 4)),
               "sample/draft/a/letters/main.md": " ".join(_edited(base, 67, 4)),
               "sample/draft/b/letters/main.md": " ".join(_edited(base, 68, 4)),
               "sample/draft/unrelated.md": " ".join(_prose(99, 300))}
    root = _tree(tmp / "generic", generic)
    _, det = _detect(root, tmp / "generic")
    assert det.events == [], det.events


def _st_shrunk_rejected_family_pair_not_reproposed(tmp: Path) -> None:
    """Rejected {a, b, c}, family shrunk to {a, b}, new d like both: {a, b, d} is refused.

    A rejection means every pair inside it is "not versions", so a group
    holding a and b together is refused even though c, and with it the full
    rejected set, is gone.
    """
    base = _prose(1, 300)
    root = _plan_tree(tmp, {"sample/draft/plan-v3.md": " ".join(_edited(base, 3, 8))})
    reg = tmp / "reg.jsonl"
    assert main(["record", "sample/draft/plan-v1.md", "sample/draft/plan-v2.md",
                 "--root", str(root), "--registry", str(reg)]) == 0
    fid = next(iter(vr.replay(vr.read_events(reg)[0]).families))
    c_sha = hashlib.sha256((root / "sample/draft/plan-v3.md").read_bytes()).hexdigest()
    vr.append({"type": "member_proposed", "family_id": fid,
               "member": {"artifact_id": "c1", "path": "sample/draft/plan-v3.md", "sha256": c_sha}}, path=reg)
    assert main(["reject", fid, "--root", str(root), "--registry", str(reg)]) == 0
    assert len(next(iter(vr.replay(vr.read_events(reg)[0]).rejections))) == 3
    vr.append({"type": "member_removed", "family_id": fid, "artifact_id": "c1"}, path=reg)
    (root / "sample/draft/plan-v3.md").unlink()
    (root / "sample/draft/plan-v4.md").write_text(" ".join(_edited(base, 4, 2)), encoding="utf-8")
    _, det = _detect(root, tmp, vr.replay(vr.read_events(reg)[0]))
    for paths in _proposed_path_sets(det.events):
        assert not {"sample/draft/plan-v1.md", "sample/draft/plan-v2.md"} <= paths, det.events
    assert any("rejected" in n for n in det.notes), det.notes


def _st_dry_run_writes_nothing(tmp: Path) -> None:
    root = _plan_tree(tmp)
    rc, out = _scan_cli(root, tmp, "--dry-run")
    assert rc == 0 and "proposed families: 1" in out, out
    written = sorted(p.name for p in tmp.iterdir() if p.name != "root")
    assert written == [], written
    rc, out = _scan_cli(root, tmp)
    assert rc == 0 and "proposed families: 1" in out, out
    written = sorted(p.name for p in tmp.iterdir() if p.name != "root")
    assert written == ["detector-cache.json", "reg.jsonl", "reg.lock", "scan-cache.json"], written


# ============================================================
# Final-review fix wave: M1-M5, S1-S5, S9, and spec coverage T1-T4
# ============================================================


def _decide(root: Path, reg: Path, fid: str, number: str) -> int:
    """`decide` the way an operator does after `show`: with the member's current SHA-256."""
    fam = vr.replay(vr.read_events(reg)[0]).families[fid]
    member = _member_by_number(fam, number)
    sha = _sha256_of(root / member.path)
    return main(["decide", fid, number, "--expect-sha", sha, "--root", str(root), "--registry", str(reg)])


def _quiet(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(list(argv))
    return rc, out.getvalue(), err.getvalue()


def _st_edited_former_actual_removed_after_new_decision(tmp: Path) -> None:
    """M1: a former actual edited in place is still ours once a newer version is decided."""
    s1, s2, s2e, s3 = _sha("v1"), _sha("v2"), _sha("v2 edited"), _sha("v3")
    events = [_ev("family_proposed", family_id="f1", source="created", members=[
                  {"artifact_id": "a1", "path": "d/v1.md", "sha256": s1},
                  {"artifact_id": "a2", "path": "d/v2.md", "sha256": s2}]),
              _ev("family_confirmed", family_id="f1"),
              _ev("decided", family_id="f1", artifact_id="a2", sha256=s2e),  # edited between proposal and decision
              _ev("member_proposed", family_id="f1", member={"artifact_id": "a3", "path": "d/v3.md", "sha256": s3}),
              _ev("member_confirmed", family_id="f1", artifact_id="a3"),
              _ev("decided", family_id="f1", artifact_id="a3", sha256=s3)]
    state = vr.replay(events)
    fp = "f" * 64
    led = memory_plan.index_ledger([_upload("d/v1.md", s1), {**_upload("d/v2.md", s2e), "header_fp": fp},
                                    _upload("d/v3.md", s3)])
    result = doc_scan.ScanResult(complete=True, files={"d/v1.md": s1, "d/v2.md": s2e, "d/v3.md": s3})
    p = memory_plan.plan(state, led, result, 1000)
    assert "d/v2.md" in p.delete and "d/v2.md" in p.skip and "d/v2.md" not in p.upload, p
    # edited again AFTER its decision and resent with a header: a hash the registry never saw
    s2ee = _sha("v2 edited again")
    led2 = memory_plan.index_ledger([_upload("d/v1.md", s1), {**_upload("d/v2.md", s2ee), "header_fp": fp},
                                     _upload("d/v3.md", s3)])
    result2 = doc_scan.ScanResult(complete=True, files={"d/v1.md": s1, "d/v2.md": s2ee, "d/v3.md": s3})
    p2 = memory_plan.plan(state, led2, result2, 1000)
    assert "d/v2.md" in p2.delete and "d/v2.md" in p2.skip, p2
    # the same unknown bytes WITHOUT a header on the ledger line are new content at a reused path
    led3 = memory_plan.index_ledger([_upload("d/v1.md", s1), _upload("d/v2.md", s2ee), _upload("d/v3.md", s3)])
    p3 = memory_plan.plan(state, led3, result2, 1000)
    assert "d/v2.md" not in p3.delete and "d/v2.md" not in p3.skip, p3
    # the decided hash alone makes it ours: sent as an ordinary file before its decision, no header
    led4 = memory_plan.index_ledger([_upload("d/v1.md", s1), _upload("d/v2.md", s2e), _upload("d/v3.md", s3)])
    p4 = memory_plan.plan(state, led4, result, 1000)
    assert "d/v2.md" in p4.delete and "d/v2.md" in p4.skip, p4
    # so does a hash a `relocated` event recorded
    moved = _sha("v1 as found at its new path")
    state5 = vr.replay(events + [_ev("relocated", artifact_id="a1", path="d/moved/v1.md", sha256=moved)])
    led5 = memory_plan.index_ledger([_upload("d/moved/v1.md", moved), _upload("d/v3.md", s3)])
    result5 = doc_scan.ScanResult(complete=True, files={"d/moved/v1.md": moved, "d/v3.md": s3})
    assert memory_plan.plan(state5, led5, result5, 1000).delete == ["d/moved/v1.md"]


def _write_lines(reg: Path, lines: list) -> None:
    reg.write_text("".join((x if isinstance(x, str) else json.dumps(x)) + "\n" for x in lines), encoding="utf-8")


def _registry_refused(reg: Path) -> bool:
    try:
        vr.replay(vr.read_events(reg)[0])
    except vr.RegistryCorrupt:
        return True
    return False


def _st_registry_event_missing_key_is_corrupt(tmp: Path) -> None:
    """M2: an event missing `seq` or a key its kind needs is corrupt, never a KeyError."""
    reg = tmp / "reg.jsonl"
    good = _ev("family_proposed", family_id="f1", source="created", members=[_m("a1"), _m("a2")])
    no_seq = {k: v for k, v in _ev("family_confirmed", family_id="f1").items() if k != "seq"}
    for bad in (no_seq,
                {k: v for k, v in _ev("decided", family_id="f1", artifact_id="a1", sha256="1" * 64).items()
                 if k != "sha256"},
                _ev("family_proposed", family_id="f2", source="created", members=[{"artifact_id": "x"}]),
                _ev("relocated", artifact_id="a1"),
                _ev("rejected", shas="not a list"),
                {**_ev("family_confirmed", family_id="f1"), "seq": "7"}):
        for lines in ([good, bad, good | {"seq": 999, "event_id": "e2", "family_id": "f9"}], [good, bad]):
            _write_lines(reg, lines)
            assert _registry_refused(reg), (bad, lines.index(bad))
    hs = _hs()
    _write_lines(reg, [good, no_seq])
    root = _tree(tmp, {"sample/draft/a.md": "a"})
    with contextlib.redirect_stderr(io.StringIO()):
        state, _result, why = hs.prepare_memory(root, reg, tmp / "cache.json", False, roots=("sample/draft",))
    assert state is None and why, (state, why)
    before = reg.read_bytes()
    try:
        vr.append({"type": "family_confirmed", "family_id": "f1"}, path=reg)
    except vr.RegistryCorrupt:
        pass
    else:
        raise AssertionError("append over a malformed last line must refuse")
    assert reg.read_bytes() == before, "a refused append writes nothing"


def _st_registry_non_object_line_is_corrupt(tmp: Path) -> None:
    """M2: a parseable line that is not a JSON object is corrupt, middle or last."""
    reg = tmp / "reg.jsonl"
    good = _ev("family_proposed", family_id="f1", source="created", members=[_m("a1")])
    for lines in ([good, "[1, 2]", good | {"seq": 999, "event_id": "e2", "family_id": "f9"}],
                  [good, "[1, 2]"], [good, "42"], [good, '"text"']):
        _write_lines(reg, lines)
        assert _registry_refused(reg), lines
    hs = _hs()
    _write_lines(reg, [good, "[1, 2]"])
    root = _tree(tmp, {"sample/draft/a.md": "a"})
    with contextlib.redirect_stderr(io.StringIO()):
        state, _result, why = hs.prepare_memory(root, reg, tmp / "cache.json", False, roots=("sample/draft",))
    assert state is None and why, (state, why)


def _push_dry(tmp: Path, root: Path, reg: Path, send: list[str], *, roots=("sample/draft",),
              ledger: list[dict] | None = None, scan=None, extra=()) -> tuple[int, str, list]:
    """`hindsight-sync.py push --dry-run --wave all` against invented fixtures, `_api` faked."""
    hs = _hs()
    keyf = tmp / "invented-key"
    keyf.write_text("invented", encoding="utf-8")
    calls: list = []
    # `ask_domains` asks Kimi through the proxy: stubbed, so a self-test sends nothing anywhere
    # (until 2026-10-12 this harness let it run, and each push scenario sent its fixture text).
    saved_hs = {n: getattr(hs, n) for n in ("_setting", "select", "get_data_root", "ledger_lines", "last_sent", "_api",
                                            "ask_domains", "load_domain_labels")}
    saved = (vr.registry_path, doc_scan.default_cache_path, doc_scope._default_roots, doc_scan.scan,
             doc_detector.default_cache_path)
    try:
        doc_detector.default_cache_path = lambda: tmp / "push-detector-cache.json"
        hs._setting = lambda name, default: str(keyf) if name == "HINDSIGHT_SYNC_KEY_FILE" else default
        hs.select = lambda _root, _wave, _mfb, _mb=0: ([(p, 1) for p in send], [])
        hs.get_data_root = lambda: root
        hs.ledger_lines = lambda: list(ledger or [])
        hs.last_sent = dict
        hs._api = lambda *a, **k: calls.append(a) or {}
        hs.ask_domains = lambda _root, _paths: {}
        hs.load_domain_labels = dict
        vr.registry_path = lambda: reg
        doc_scan.default_cache_path = lambda: tmp / "push-scan-cache.json"
        doc_scope._default_roots = lambda: tuple(roots)
        if scan is not None:
            doc_scan.scan = scan
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = hs.main(["push", "--dry-run", "--wave", "all", *extra])
    finally:
        for n, v in saved_hs.items():
            setattr(hs, n, v)
        (vr.registry_path, doc_scan.default_cache_path, doc_scope._default_roots, doc_scan.scan,
         doc_detector.default_cache_path) = saved
    return rc, err.getvalue(), calls


def _st_corrupt_registry_push_sends_non_family_files(tmp: Path) -> None:
    """M2: push over a malformed registry crashes nothing and still sends what no family can hold."""
    root = _tree(tmp, {"sample/draft/a.md": "a", "notes/c.md": "c"})
    reg = tmp / "reg.jsonl"
    good = _ev("family_proposed", family_id="f1", source="created", members=[_m("a1")])
    for bad in ("[1, 2]", {k: v for k, v in good.items() if k != "seq"} | {"event_id": "e9"}):
        _write_lines(reg, [good, bad])
        rc, err, calls = _push_dry(tmp, root, reg, ["sample/draft/a.md", "notes/c.md"])
        assert rc == 0 and all(c[0] == "GET" for c in calls), (rc, err)  # the failed-operations listing only (HOS-200)
        assert "upload notes/c.md" in err and "upload sample/draft/a.md" not in err, err


def _st_reject_refuses_confirmed_family(tmp: Path) -> None:
    """M3a: `reject` on a confirmed family refuses and points to remove-member."""
    root, reg = _tree(tmp, {"d/a-v1.md": "one", "d/a-v2.md": "two"}), tmp / "reg.jsonl"
    _quiet("record", "d/a-v1.md", "d/a-v2.md", "--root", str(root), "--registry", str(reg))
    fid = next(iter(vr.replay(vr.read_events(reg)[0]).families))
    _quiet("confirm", fid, "--registry", str(reg))
    before = reg.read_bytes()
    rc, _out, err = _quiet("reject", fid, "--root", str(root), "--registry", str(reg))
    assert rc == 2 and "remove-member" in err and reg.read_bytes() == before, (rc, err)


def _st_reject_paths_refuses_confirmed_members(tmp: Path) -> None:
    """A path already a confirmed member must refuse `reject` by paths too.

    Without this, `reject <confirmed-path> <other-path>` would key a `rejected`
    event by content and leave the confirmed family's path free to be proposed
    again into a second, dead-end family, exactly what `_confirmed_owner`
    already exists to prevent for `record`.
    """
    root, reg = _tree(tmp, {"d/a-v1.md": "one", "d/a-v2.md": "two", "d/c.md": "three"}), tmp / "reg.jsonl"
    _quiet("record", "d/a-v1.md", "d/a-v2.md", "--root", str(root), "--registry", str(reg))
    fid = next(iter(vr.replay(vr.read_events(reg)[0]).families))
    _quiet("confirm", fid, "--registry", str(reg))
    before = reg.read_bytes()
    rc, _out, err = _quiet("reject", "d/a-v1.md", "d/c.md", "--root", str(root), "--registry", str(reg))
    assert rc == 2 and "remove-member" in err and fid in err and reg.read_bytes() == before, (rc, err)


def _st_path_in_two_confirmed_families_is_corrupt(tmp: Path) -> None:
    """M3b: no path may be a member of two confirmed families."""
    events = [_ev("family_proposed", family_id="f1", source="created",
                  members=[{"artifact_id": "a1", "path": "d/x.md", "sha256": _sha("x")}, _m("a2")]),
              _ev("family_proposed", family_id="f2", source="created",
                  members=[{"artifact_id": "b1", "path": "d/x.md", "sha256": _sha("x")}, _m("b2")]),
              _ev("family_confirmed", family_id="f1")]
    vr.replay(events)  # one confirmed family holding the path is fine
    try:
        vr.replay(events + [_ev("family_confirmed", family_id="f2")])
    except vr.RegistryCorrupt:
        return
    raise AssertionError("a path in two confirmed families must be corrupt")


def _st_plan_never_deletes_what_it_uploads_or_holds(tmp: Path) -> None:
    """M3c: a delete whose ledger path the same plan uploads or holds is dropped."""
    x, y, z, w = _sha("x"), _sha("y"), _sha("z"), _sha("w")
    st = vr.State()
    st.families["f1"] = vr.Family("f1", confirmed=True, members={"a1": vr.Member("a1", "d/x.md", x)},
                                  actual="a1", actual_sha=x)
    st.families["f2"] = vr.Family("f2", confirmed=True, members={
        "b1": vr.Member("b1", "d/x.md", x), "b2": vr.Member("b2", "d/y.md", y), "b3": vr.Member("b3", "d/z.md", z)},
        actual="b2", actual_sha=y)
    st.families["f3"] = vr.Family("f3", confirmed=True, members={"c1": vr.Member("c1", "d/w.md", w)},
                                  proposed={"c2": vr.Member("c2", "d/z.md", z)}, actual="c1", actual_sha=w)
    led = memory_plan.index_ledger([_upload(q, s) for q, s in (("d/x.md", x), ("d/y.md", y), ("d/z.md", z),
                                                                 ("d/w.md", w))])
    result = doc_scan.ScanResult(complete=True, files={"d/x.md": x, "d/y.md": y, "d/z.md": z, "d/w.md": w})
    p = memory_plan.plan(st, led, result, 100_000)
    assert "d/x.md" in p.upload and "d/z.md" in p.hold, p
    assert "d/x.md" not in p.delete and "d/z.md" not in p.delete, p.delete


def _st_decide_requires_expect_sha(tmp: Path) -> None:
    """M4: `decide` without `--expect-sha` refuses with rc 2 and decides nothing."""
    root, reg = _tree(tmp, {"d/a-v1.md": "one", "d/a-v2.md": "two"}), tmp / "reg.jsonl"
    _quiet("record", "d/a-v1.md", "d/a-v2.md", "--root", str(root), "--registry", str(reg))
    fid = next(iter(vr.replay(vr.read_events(reg)[0]).families))
    _quiet("confirm", fid, "--registry", str(reg))
    rc, _out, err = _quiet("decide", fid, "2", "--root", str(root), "--registry", str(reg))
    assert rc == 2 and "--expect-sha" in err, (rc, err)
    assert vr.replay(vr.read_events(reg)[0]).families[fid].actual is None


def _st_show_prints_full_sha_and_decide_command(tmp: Path) -> None:
    """M4: `show` prints each member's full CURRENT SHA-256 and the exact decide command."""
    root, reg = _tree(tmp, {"d/a-v1.md": "one", "d/a-v2.md": "two"}), tmp / "reg.jsonl"
    _quiet("record", "d/a-v1.md", "d/a-v2.md", "--root", str(root), "--registry", str(reg))
    fid = next(iter(vr.replay(vr.read_events(reg)[0]).families))
    _quiet("confirm", fid, "--registry", str(reg))
    (root / "d/a-v2.md").write_text("two, edited after record", encoding="utf-8")
    current = _sha("two, edited after record")
    rc, out, _err = _quiet("show", fid, "--root", str(root), "--registry", str(reg))
    assert rc == 0 and f"sha256 now: {current}" in out and f"sha256 now: {_sha('one')}" in out, out
    cmd = f"decide {fid} 2 --expect-sha {current}"
    assert cmd in out, out
    rc2, _o, err2 = _quiet(*cmd.split(), "--root", str(root), "--registry", str(reg))
    fam = vr.replay(vr.read_events(reg)[0]).families[fid]
    assert rc2 == 0 and fam.actual_sha == current, (rc2, err2)


def _st_caches_under_runtime_cache_with_pid_tmp(tmp: Path) -> None:
    """M5: both caches resolve `runtime:hindsight-sync/cache` and write through `.tmp.<pid>`."""
    det = doc_detector
    names: list[str] = []
    saved = (doc_scan.data_path, det.data_path, os.replace)
    sources: list[str] = []
    try:
        doc_scan.data_path = det.data_path = lambda name: names.append(name) or tmp
        assert doc_scan.default_cache_path().parent == tmp and det.default_cache_path().parent == tmp
        os.replace = lambda src, dst: sources.append(Path(src).name) or saved[2](src, dst)
        doc_scan._write_cache(tmp / "scan.json", {}, 0)
        det.save_cache(tmp / "det.json", {})
    finally:
        doc_scan.data_path, det.data_path, os.replace = saved
    assert names == ["runtime:hindsight-sync/cache"] * 2, names
    assert sources == [f"scan.json.tmp.{os.getpid()}", f"det.json.tmp.{os.getpid()}"], sources


def _st_push_prints_scan_errors(tmp: Path) -> None:
    """S1: every pass prints the scan's incomplete reasons on stderr."""
    root = _tree(tmp, {"notes/c.md": "c"})
    reg = tmp / "reg.jsonl"
    vr.append({"type": "family_proposed", "family_id": "f1", "source": "created", "members": [_m("a1")]}, path=reg)
    fake = lambda *a, **k: doc_scan.ScanResult(complete=False, errors=["invented walk error"])  # noqa: E731
    rc, err, _calls = _push_dry(tmp, root, reg, ["notes/c.md"], scan=fake)
    assert rc == 0 and "invented walk error" in err, err


def _st_push_empty_registry_not_reported_as_scan_error(tmp: Path) -> None:
    """An empty registry (no families proposed yet) is not a scan failure: `push` must not
    print "no families" as though a scan went wrong, on every single pass while the
    registry is still empty. Companion to `_st_push_prints_scan_errors` above: that one
    pins a REAL scan error still prints; this one pins the registry-empty case does not."""
    root = _tree(tmp, {"notes/c.md": "c"})
    reg = tmp / "reg.jsonl"  # never created: read_events() sees no events, so no families
    rc, err, _calls = _push_dry(tmp, root, reg, ["notes/c.md"])
    assert rc == 0 and "no families" not in err, err


def _st_locked_timeout_gives_up(tmp: Path) -> None:
    """S2: `locked(timeout=...)` gives up with `LockTimeout` instead of waiting forever."""
    import fcntl
    reg = tmp / "reg.jsonl"
    with open(reg.with_suffix(".lock"), "a") as other:
        fcntl.flock(other, fcntl.LOCK_EX)
        t0 = time.monotonic()
        try:
            with vr.locked(reg, timeout=0.3):
                raise AssertionError("took a lock another holder has")
        except vr.LockTimeout:
            pass
        assert time.monotonic() - t0 < 5, "gave up too late"
        assert not vr._LOCK_DEPTH, vr._LOCK_DEPTH
    with vr.locked(reg, timeout=0.3):
        pass


def _hold_lock(lock: str, seconds: float, ready) -> None:
    import fcntl
    with open(lock, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        ready.set()
        time.sleep(seconds)


def _st_push_skips_registry_when_lock_busy(tmp: Path) -> None:
    """S2: a registry lock held by another process skips the registry part of the pass; nothing waits for it."""
    hs = _hs()
    root = _tree(tmp, {"sample/draft/a.md": "a"})
    reg = tmp / "reg.jsonl"
    vr.append({"type": "family_proposed", "family_id": "f1", "source": "created", "members": [_m("a1")]}, path=reg)
    before = reg.read_bytes()
    saved = getattr(hs, "REGISTRY_LOCK_TIMEOUT", None)
    calls: list = []
    saved_api = hs._api
    ctx = multiprocessing.get_context("fork")
    ready = ctx.Event()
    holder = ctx.Process(target=_hold_lock, args=(str(reg.with_suffix(".lock")), 4.0, ready))
    holder.start()
    try:
        assert ready.wait(10), "the lock holder never took the lock"
        hs.REGISTRY_LOCK_TIMEOUT = 0.3
        hs._api = lambda *a, **k: calls.append(a) or {}
        t0 = time.monotonic()
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            state, _result, why = hs.prepare_memory(root, reg, tmp / "cache.json", False, roots=("sample/draft",))
            plan = memory_plan.Plan(delete=["x"], delete_path={"x": "x"})
            rc1 = hs.deletion_phase(reg, 1, plan, lambda st: plan, "http://invented.invalid", "b", "k",
                                    read_state=lambda: vr.State(generation=1))
            rc2 = hs.run_deletions(plan, "http://invented.invalid", "b", "k", 1, reg,
                                   read_state=lambda: vr.State(generation=1))
        waited = time.monotonic() - t0
    finally:
        hs.REGISTRY_LOCK_TIMEOUT = saved
        hs._api = saved_api
        holder.join(timeout=30)
    assert state is None and why and "lock" in why, (state, why)
    assert rc1 == 0 and rc2 == 0 and calls == [] and reg.read_bytes() == before, (rc1, rc2, calls)
    assert waited < 3.5, f"waited {waited:.1f} s for a lock the timer must not wait on"


def _st_record_stores_canonical_relative_path(tmp: Path) -> None:
    """S3: `record` stores the root-relative POSIX path of the real file, never the typed one."""
    root, reg = _tree(tmp, {"d/a-v1.md": "one", "d/x/a-v2.md": "two"}), tmp / "reg.jsonl"
    rc, _o, err = _quiet("record", "d/x/../a-v1.md", "./d/x/a-v2.md", "--root", str(root), "--registry", str(reg))
    fam = next(iter(vr.replay(vr.read_events(reg)[0]).families.values()))
    assert rc == 0 and sorted(m.path for m in fam.proposed.values()) == ["d/a-v1.md", "d/x/a-v2.md"], (rc, err, fam)


def _st_restore_refuses_history_and_held(tmp: Path) -> None:
    """S4: `restore` refuses a path push would hold or treat as history (rc 2)."""
    root = _tree(tmp, {"d/a-v1.md": "one", "d/a-v2.md": "two", "d/a-v3.md": "three", "d/b.md": "b"})
    reg, ledger = tmp / "reg.jsonl", tmp / "ledger.jsonl"
    _quiet("record", "d/a-v1.md", "d/a-v2.md", "--root", str(root), "--registry", str(reg))
    fid = next(iter(vr.replay(vr.read_events(reg)[0]).families))
    _quiet("confirm", fid, "--registry", str(reg))
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        assert _decide(root, reg, fid, "2") == 0
    vr.append({"type": "member_proposed", "family_id": fid,
               "member": {"artifact_id": "p3", "path": "d/a-v3.md", "sha256": _sha("three")}}, path=reg)
    common = ("--root", str(root), "--registry", str(reg), "--ledger", str(ledger))
    for rel, want in (("d/a-v1.md", 2), ("d/a-v3.md", 2), ("d/a-v2.md", 0), ("d/b.md", 0)):
        rc, _o, err = _quiet("restore", rel, *common)
        assert rc == want, (rel, rc, err)
    lines = [json.loads(x)["path"] for x in ledger.read_text(encoding="utf-8").splitlines()]
    assert lines == ["d/a-v2.md", "d/b.md"], lines


def _st_commands_resolve_inside_lock(tmp: Path) -> None:
    """S5: confirm, reject, remove-member and summary read the registry only under its lock."""
    root = _tree(tmp, {"d/a-v1.md": "one", "d/a-v2.md": "two", "d/b-v1.md": "b1", "d/b-v2.md": "b2"})
    reg = tmp / "reg.jsonl"
    _quiet("record", "d/a-v1.md", "d/a-v2.md", "--root", str(root), "--registry", str(reg))
    _quiet("record", "d/b-v1.md", "d/b-v2.md", "--root", str(root), "--registry", str(reg))
    st = vr.replay(vr.read_events(reg)[0])
    fa = next(f for f in st.families.values() if any(m.path == "d/a-v1.md" for m in f.proposed.values()))
    fb = next(f for f in st.families.values() if f.family_id != fa.family_id)
    key = str(reg.with_suffix(".lock").resolve())
    outside: list[str] = []
    saved = vr.read_events

    def checked(path=None, _cmd=None):
        if not vr._LOCK_DEPTH.get(key):
            outside.append(current[0])
        return saved(path)

    current = [""]
    try:
        vr.read_events = checked
        for argv in (("confirm", fa.family_id), ("summary", fa.family_id, "1", "2", "invented text"),
                     ("remove-member", fa.family_id, "2"), ("reject", fb.family_id)):
            current[0] = argv[0]
            rc, _o, err = _quiet(*argv, "--root", str(root), "--registry", str(reg))
            assert rc == 0, (argv, err)
    finally:
        vr.read_events = saved
    assert outside == [], outside


def _st_prime_check_spawns_venv_python(tmp: Path) -> None:
    """S9: the /prime doc-versions check spawns the workspace's `.venv/bin/python`."""
    import importlib.util
    import subprocess
    from scripts.utils.paths import get_workspace_root
    spec = importlib.util.spec_from_file_location(
        "_doc_versions_prime", get_workspace_root() / "scripts" / "prime-health-parallel.py")
    prime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prime)
    ws = tmp / "ws"
    (ws / "scripts").mkdir(parents=True)
    (ws / "scripts" / "doc-versions.py").write_text("# invented\n", encoding="utf-8")
    (ws / ".venv" / "bin").mkdir(parents=True)
    (ws / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
    seen: list = []
    prime.subprocess = type("S", (), {"run": staticmethod(
        lambda argv, **kw: seen.append(argv) or subprocess.CompletedProcess(argv, 0, "3\n", ""))})
    got = prime.run_doc_versions_pending(ws)
    assert seen and seen[0][0] == str(ws / ".venv" / "bin" / "python"), seen
    assert got["status"] == "ok" and "3" in got["output"], got


def _tree_snapshot(root: Path) -> dict[str, tuple]:
    return {p.relative_to(root).as_posix(): (p.read_bytes() if p.is_file() else None, p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*"))}


def _st_source_files_never_change(tmp: Path) -> None:
    """T1: record, confirm, decide, scan (dry and not), plan and push --dry-run leave the documents byte-identical."""
    root = _plan_tree(tmp, {"notes/c.md": "outside every family"})
    reg = tmp / "reg.jsonl"
    before = _tree_snapshot(root)
    rc, _ = _scan_cli(root, tmp, "--dry-run")
    assert rc == 0
    rc, _ = _scan_cli(root, tmp)
    assert rc == 0
    fid = next(iter(vr.replay(vr.read_events(reg)[0]).families))
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        assert main(["confirm", fid, "--registry", str(reg)]) == 0
        assert _decide(root, reg, fid, "2") == 0
        assert main(["record", "sample/draft/unrelated.md", "notes/c.md", "--root", str(root),
                     "--registry", str(reg)]) == 0
    state = vr.replay(vr.read_events(reg)[0])
    result = doc_scan.scan(root, tmp / "t1-cache.json", roots=("sample/draft",), write_cache=True)
    memory_plan.plan(state, memory_plan.index_ledger([]), result, 0, root=root)
    rc, err, calls = _push_dry(tmp, root, reg, ["sample/draft/plan-v1.md", "sample/draft/plan-v2.md", "notes/c.md"])
    assert rc == 0 and all(c[0] == "GET" for c in calls), err  # the failed-operations listing only (HOS-200)
    assert _tree_snapshot(root) == before, "a source document changed"


def _st_relocated_history_changes_header(tmp: Path) -> None:
    """T2: a history member moved while the actual is unchanged changes the header and resends the actual."""
    hs = _hs()
    root = _tree(tmp, {"d/new.md": "new text", "d/archive-moved/old.md": "old text"})
    old, new = _sha("old text"), _sha("new text")
    events = _decided_family({"artifact_id": "h1", "path": "d/old.md", "sha256": old},
                             {"artifact_id": "a1", "path": "d/new.md", "sha256": new}, actual="a1")
    result = doc_scan.ScanResult(complete=True, files={"d/new.md": new})
    p1 = memory_plan.plan(vr.replay(events), {}, result, 0, root=root)
    seen = {"d/new.md": {**hs._ledger_meta(root / "d/new.md"), "path": "d/new.md",
                         "header_fp": p1.fingerprints["d/new.md"]}}
    assert not hs.needs_send(root, "d/new.md", seen, p1.fingerprints)
    events.append(_ev("relocated", artifact_id="h1", path="d/archive-moved/old.md", sha256=old))
    p2 = memory_plan.plan(vr.replay(events), {}, result, 0, root=root)
    assert "d/archive-moved/old.md" in p2.upload["d/new.md"] and " d/old.md" not in p2.upload["d/new.md"], p2.upload
    assert p2.fingerprints["d/new.md"] != p1.fingerprints["d/new.md"]
    assert hs.needs_send(root, "d/new.md", seen, p2.fingerprints), "a moved history member must resend the actual"


def _st_rollback_after_membership_change(tmp: Path) -> None:
    """T3: remove a member, then decide back to an earlier one; numbering and memory follow."""
    root = _tree(tmp, {"d/a-v1.md": "one", "d/a-v2.md": "two", "d/a-v3.md": "three"})
    reg = tmp / "reg.jsonl"
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        main(["record", "d/a-v1.md", "d/a-v2.md", "--root", str(root), "--registry", str(reg)])
        fid = next(iter(vr.replay(vr.read_events(reg)[0]).families))
        vr.append({"type": "member_proposed", "family_id": fid,
                   "member": {"artifact_id": "p3", "path": "d/a-v3.md", "sha256": _sha("three")}}, path=reg)
        main(["confirm", fid, "--registry", str(reg)])
        assert _decide(root, reg, fid, "3") == 0
        assert main(["remove-member", fid, "2", "--registry", str(reg)]) == 0  # d/a-v2.md, a mistaken inclusion
    rc, out, _err = _quiet("show", fid, "--root", str(root), "--registry", str(reg))
    assert rc == 0 and "1. d/a-v1.md" in out and "2. d/a-v3.md" in out and "d/a-v2.md" not in out, out
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        assert _decide(root, reg, fid, "1") == 0
    fam = vr.replay(vr.read_events(reg)[0]).families[fid]
    assert fam.members[fam.actual].path == "d/a-v1.md" and fam.actual_sha == _sha("one"), fam
    state = vr.replay(vr.read_events(reg)[0])
    led = memory_plan.index_ledger([_upload(q, _sha(t)) for q, t in
                                    (("d/a-v1.md", "one"), ("d/a-v2.md", "two"), ("d/a-v3.md", "three"))])
    result = doc_scan.ScanResult(complete=True, files={q: _sha(t) for q, t in
                                                       (("d/a-v1.md", "one"), ("d/a-v2.md", "two"),
                                                        ("d/a-v3.md", "three"))})
    p = memory_plan.plan(state, led, result, 1000, root=root)
    assert "d/a-v1.md" in p.upload and "d/a-v3.md" in p.upload["d/a-v1.md"], p.upload
    assert "d/a-v2.md" not in p.upload["d/a-v1.md"], "a removed member is not an earlier version"
    assert p.delete == ["d/a-v3.md"], p.delete
    assert "d/a-v2.md" not in p.skip and "d/a-v2.md" not in p.hold, "a removed member is an ordinary file again"


def _t4_decider(root: str, reg: str, fid: str, n: int) -> None:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        for i in range(n):
            if _decide(Path(root), Path(reg), fid, "1" if i % 2 else "2") != 0:
                raise SystemExit(3)


def _t4_scanner(root: str, tmp: str, n: int) -> None:
    for _ in range(n):
        rc, _out = _scan_cli(Path(root), Path(tmp))
        if rc != 0:
            raise SystemExit(4)


def _st_concurrent_decide_and_scan(tmp: Path) -> None:
    """T4: a `decide` loop and a `scan` loop in two processes on one registry; it replays clean."""
    root = _plan_tree(tmp, {"d/a-v1.md": "one", "d/a-v2.md": "two",
                            "sample/draft/memo-v1.md": " ".join(_prose(7, 300)),
                            "sample/draft/memo-v2.md": " ".join(_edited(_prose(7, 300), 3, 8))})
    reg = tmp / "reg.jsonl"
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        main(["record", "d/a-v1.md", "d/a-v2.md", "--root", str(root), "--registry", str(reg)])
        fid = next(iter(vr.replay(vr.read_events(reg)[0]).families))
        main(["confirm", fid, "--registry", str(reg)])
    n = 12
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_t4_decider, args=(str(root), str(reg), fid, n)),
             ctx.Process(target=_t4_scanner, args=(str(root), str(tmp), 4))]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=120)
        assert proc.exitcode == 0, f"worker exited with {proc.exitcode!r}"
    events, warnings = vr.read_events(reg)
    assert not warnings, warnings
    state = vr.replay(events)
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert sum(1 for e in events if e["type"] == "decided") == n
    assert sum(1 for e in events if e["type"] == "family_proposed") == 3, "record plus two scan proposals, once each"
    assert state.families[fid].actual is not None


# ============================================================
# Self-test scenarios (HOS-178 finish, 2026-10-12)
# ============================================================


def _st_merge_folds_and_dismisses(tmp: Path) -> None:
    """One revision line proposed as two families becomes one; the emptied one is dismissed."""
    root = _tree(tmp, {"d/x-r1.md": "1", "d/x-r2.md": "2", "d/x-r3.md": "3", "d/x-r4.md": "4"})
    reg = tmp / "reg.jsonl"
    main(["record", "d/x-r1.md", "d/x-r2.md", "--root", str(root), "--registry", str(reg)])
    main(["record", "d/x-r3.md", "d/x-r4.md", "--root", str(root), "--registry", str(reg)])
    st = vr.replay(vr.read_events(reg)[0])
    into, src = sorted(st.families, key=lambda f: min(m.path for m in st.families[f].proposed.values()))
    assert main(["merge", into, src, "--registry", str(reg)]) == 0
    st = vr.replay(vr.read_events(reg)[0])
    assert list(st.families) == [into] and src in st.dismissed, (st.families, st.dismissed)
    assert {m.path for m in st.families[into].proposed.values()} == {f"d/x-r{i}.md" for i in range(1, 5)}
    assert main(["confirm", src, "--registry", str(reg)]) == 2, "a dismissed family takes no event"
    before = reg.read_bytes()
    assert main(["merge", into, into, "--registry", str(reg)]) == 2
    assert main(["dismiss", into, "--registry", str(reg)]) == 2, "four members: not dismissable"
    assert reg.read_bytes() == before, "refusals write nothing"


def _st_merge_refuses_confirmed_source(tmp: Path) -> None:
    root = _tree(tmp, {"d/a1.md": "1", "d/a2.md": "2", "d/b1.md": "3", "d/b2.md": "4"})
    reg = tmp / "reg.jsonl"
    main(["record", "d/a1.md", "d/a2.md", "--root", str(root), "--registry", str(reg)])
    main(["record", "d/b1.md", "d/b2.md", "--root", str(root), "--registry", str(reg)])
    fa, fb = sorted(vr.replay(vr.read_events(reg)[0]).families,
                    key=lambda f: min(m.path for m in vr.replay(vr.read_events(reg)[0]).families[f].proposed.values()))
    main(["confirm", fb, "--registry", str(reg)])
    n = len(vr.read_events(reg)[0])
    assert main(["merge", fa, fb, "--registry", str(reg)]) == 2
    assert len(vr.read_events(reg)[0]) == n
    assert main(["merge", fb, fa, "--registry", str(reg)]) == 0, "a confirmed family may receive members"
    fam = vr.replay(vr.read_events(reg)[0]).families[fb]
    assert fam.confirmed and len(fam.members) == 2 and len(fam.proposed) == 2, fam


def _st_dismiss_only_single_member_unconfirmed(tmp: Path) -> None:
    """`dismiss` takes a family under two members out of `pending`, and nothing else."""
    root = _tree(tmp, {"d/a1.md": "1", "d/a2.md": "2"})
    reg = tmp / "reg.jsonl"
    main(["record", "d/a1.md", "d/a2.md", "--root", str(root), "--registry", str(reg)])
    fid = next(iter(vr.replay(vr.read_events(reg)[0]).families))
    assert main(["dismiss", fid, "--registry", str(reg)]) == 2, "two members: reject or merge"
    main(["remove-member", fid, "2", "--registry", str(reg)])
    assert main(["dismiss", fid, "--registry", str(reg)]) == 0
    st = vr.replay(vr.read_events(reg)[0])
    assert fid in st.dismissed and not st.families and not st.rejections, st
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        main(["pending", "--count", "--registry", str(reg)])
    assert out.getvalue().strip() == "0", out.getvalue()
    events = vr.read_events(reg)[0]
    again = {**_ev("family_proposed", family_id=fid, source="x", members=[]), "seq": events[-1]["seq"] + 1}
    confirmed = [_ev("family_proposed", family_id="c1", source="x", members=[_m("c")]),
                 _ev("family_confirmed", family_id="c1"), _ev("family_dismissed", family_id="c1", reason="x")]
    for bad, why in ((events + [again], "a dismissed family_id was declared again"),
                     (confirmed, "a confirmed family was dismissed")):
        try:
            vr.replay(bad)
            raise AssertionError(why)
        except vr.RegistryCorrupt:
            pass
    reg2 = tmp / "reg2.jsonl"
    main(["record", "d/a1.md", "d/a2.md", "--root", str(root), "--registry", str(reg2)])
    fid2 = next(iter(vr.replay(vr.read_events(reg2)[0]).families))
    main(["confirm", fid2, "--registry", str(reg2)])
    main(["remove-member", fid2, "2", "--registry", str(reg2)])
    assert main(["dismiss", fid2, "--registry", str(reg2)]) == 2, "a confirmed family is never dismissed"


def _st_pending_skips_rejected_family(tmp: Path) -> None:
    root = _tree(tmp, {"d/a1.md": "1", "d/a2.md": "2", "d/b1.md": "3", "d/b2.md": "4"})
    reg = tmp / "reg.jsonl"
    main(["record", "d/a1.md", "d/a2.md", "--root", str(root), "--registry", str(reg)])
    main(["record", "d/b1.md", "d/b2.md", "--root", str(root), "--registry", str(reg)])
    st = vr.replay(vr.read_events(reg)[0])
    rejected = next(f for f in st.families if any(m.path == "d/a1.md" for m in st.families[f].proposed.values()))
    main(["reject", rejected, "--root", str(root), "--registry", str(reg)])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        main(["pending", "--registry", str(reg)])
    assert rejected not in out.getvalue() and len(out.getvalue().splitlines()) == 1, out.getvalue()


def _archive_tree(tmp: Path):
    """A confirmed family: `plan-v2.md` actual, `_archive/plan-v1.md` history beside it."""
    root = _plan_tree(tmp)
    (root / "sample/draft/_archive").mkdir()
    (root / "sample/draft/plan-v1.md").rename(root / "sample/draft/_archive/plan-v1.md")
    (root / "old-archive").mkdir()  # archive-shaped, outside every scope root
    (root / "old-archive/a.md").write_text("top-level archive", encoding="utf-8")
    old = _sha256_of(root / "sample/draft/_archive/plan-v1.md")
    new = _sha256_of(root / "sample/draft/plan-v2.md")
    state = vr.replay(_decided_family(
        {"artifact_id": "h1", "path": "sample/draft/_archive/plan-v1.md", "sha256": old},
        {"artifact_id": "a1", "path": "sample/draft/plan-v2.md", "sha256": new}, actual="a1"))
    return root, state, old, new


def _st_archived_member_scanned_never_proposed(tmp: Path) -> None:
    """A confirmed member in a nested archive is hashed into `archived`, never `files`."""
    root, state, old, _new = _archive_tree(tmp)
    members = doc_scan.confirmed_member_paths(state) | {"old-archive/a.md"}
    result = doc_scan.scan(root, tmp / "c.json", roots=("sample/draft",), write_cache=False, members=members)
    assert result.complete, result.errors
    arch = "sample/draft/_archive/plan-v1.md"
    assert result.archived == {arch: old} and arch not in result.files, result.archived
    assert result.archived_scope == frozenset({arch}), "an archive outside every scope root stays out"
    det = doc_detector.detect(vr.State(), result, root)
    assert not any(arch in json.dumps(ev) for ev in det.events), det.events
    events, _ = doc_scan.availability_events(state, set(), result)
    assert events == [] and result.out_of_scope == [], (events, result.out_of_scope)
    (root / arch).unlink()
    gone = doc_scan.scan(root, tmp / "c.json", roots=("sample/draft",), write_cache=False, members=members)
    events, _ = doc_scan.availability_events(state, set(), gone)
    assert events == [{"type": "unavailable", "artifact_id": "h1"}], events
    plain = doc_scan.scan(root, tmp / "c.json", roots=("sample/draft",), write_cache=False)
    doc_scan.availability_events(state, set(), plain)
    assert plain.out_of_scope == ["h1"], "without members the old rule holds"


def _st_archived_history_deleted_when_ledger_matches(tmp: Path) -> None:
    root, state, old, new = _archive_tree(tmp)
    arch = "sample/draft/_archive/plan-v1.md"
    led = memory_plan.index_ledger([_upload(arch, old), _upload("sample/draft/plan-v2.md", new)])
    result = doc_scan.scan(root, tmp / "c.json", roots=("sample/draft",), write_cache=False,
                           members=doc_scan.confirmed_member_paths(state))
    doc_scan.availability_events(state, set(), result)
    p = memory_plan.plan(state, led, result, 100, root=root)
    assert p.delete == [arch] and arch in p.skip and arch not in p.hold, p
    (root / arch).write_text("someone else's text", encoding="utf-8")
    result = doc_scan.scan(root, tmp / "c.json", roots=("sample/draft",), write_cache=False,
                           members=doc_scan.confirmed_member_paths(state))
    assert memory_plan.plan(state, led, result, 100, root=root).delete == [], "a reused archive path stays"


def _st_txt_actual_gets_header(tmp: Path) -> None:
    """A `.txt` file is sent as itself, so a `.txt` actual carries the header and its history goes."""
    old, new = _sha("old"), _sha("new")
    state = vr.replay(_decided_family({"artifact_id": "h1", "path": "d/n-old.txt", "sha256": old},
                                      {"artifact_id": "a1", "path": "d/n.txt", "sha256": new}, actual="a1"))
    led = memory_plan.index_ledger([_upload("d/n-old.txt", old), _upload("d/n.txt", new)])
    result = doc_scan.ScanResult(complete=True, files={"d/n-old.txt": old, "d/n.txt": new})
    p = memory_plan.plan(state, led, result, 100)
    assert "d/n.txt" in p.upload and p.delete == ["d/n-old.txt"], p
    assert {"txt", "md"} <= _hs().TEXT_EXT == {e.lstrip(".") for e in memory_plan.SENT_AS_ITSELF}, _hs().TEXT_EXT


def _st_daily_cap_counts_family_deletions_only(tmp: Path) -> None:
    now = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)
    recent, old = "2026-01-02T08:00:00+00:00", "2026-01-01T08:00:00+00:00"
    lines = ([{"path": "d/a.md", "delete_intent": f"d/a{i}.md", "generation": 7, "at": recent} for i in range(3)]
             + [{"path": "d/b.md", "delete_intent": "d/b.md", "generation": 7, "at": old}]
             + [{"path": "d/c.md", "delete_intent": "d/c.md", "mirror": "gone", "at": recent}]
             + [{"path": "d/e.md", "deleted": "d/e.md", "generation": 7, "at": recent}]
             + [{"path": "d/f.md", "delete_intent": "d/f.md", "generation": 7, "at": "garbled"}])
    since = now - timedelta(hours=24)
    assert memory_plan.family_deletions_since(lines, since) == 4
    p = memory_plan.Plan(delete=[f"id{i}" for i in range(5)], delete_path={f"id{i}": "x" for i in range(5)})
    p = memory_plan.cap_daily(p, used=38, cap=40)
    assert p.delete == ["id0", "id1"] and set(p.delete_path) == {"id0", "id1"}, p
    assert any("3 planned deletion(s) wait" in n for n in p.notes), p.notes
    assert memory_plan.cap_daily(memory_plan.Plan(delete=["a"], delete_path={"a": "x"}), used=99).delete == []
    q = memory_plan.Plan(delete=["a"], delete_path={"a": "x"})
    assert memory_plan.cap_daily(q, used=0) is q and q.delete == ["a"] and not q.notes


def _st_push_detect_proposes_before_planning(tmp: Path) -> None:
    """`push --detect --dry-run` prints the proposals, writes no registry, and plans from them."""
    root = _plan_tree(tmp)
    reg = tmp / "reg.jsonl"
    rc, err, calls = _push_dry(tmp, root, reg, ["sample/draft/plan-v1.md"], extra=["--detect"])
    assert rc == 0 and all(c[0] == "GET" for c in calls), (rc, calls)
    assert "detector proposes new family" in err and "(not written, dry run)" in err, err
    assert not reg.exists(), "a dry run writes no registry"
    rc, err, _ = _push_dry(tmp, root, reg, ["sample/draft/plan-v1.md"])
    assert "detector" not in err, "without --detect the detector does not run"
    hs = _hs()
    saved = doc_detector.default_cache_path
    doc_detector.default_cache_path = lambda: tmp / "det.json"
    try:
        result = doc_scan.scan(root, tmp / "c.json", roots=("sample/draft",), write_cache=False)
        with contextlib.redirect_stderr(io.StringIO()):
            st = hs.run_detector(root, reg, vr.State(), result, dry_run=False)
            again = hs.run_detector(root, reg, st, result, dry_run=False)
    finally:
        doc_detector.default_cache_path = saved
    assert len(st.families) == 1 and len(again.families) == 1, "appended once, idempotent after"
    assert _proposed_path_sets(vr.read_events(reg)[0]) == [
        {"sample/draft/plan-v1.md", "sample/draft/plan-v2.md"}]


def _st_timer_template_detects_and_never_deletes(tmp: Path) -> None:
    from scripts.utils.paths import get_workspace_root
    unit = (get_workspace_root() / "scripts/templates/systemd/hindsight-sync.service").read_text(encoding="utf-8")
    exec_line = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    assert "--detect" in exec_line and "--allow-deletes" not in exec_line, exec_line


SELF_TESTS = [
    _st_truncated_tail_tolerated,
    _st_append_after_truncated_tail_recovers,
    _st_append_after_unterminated_valid_line_keeps_it,
    _st_corrupt_middle_blocks,
    _st_one_actual_per_family,
    _st_artifact_in_two_confirmed_families_is_corrupt,
    _st_rejection_keyed_by_content,
    _st_locked_is_reentrant,
    _st_append_refuses_invariant_break,
    _st_random_sequences_keep_invariants,
    _st_concurrent_appends_keep_order,
    _st_record_creates_proposed_family,
    _st_decide_refuses_changed_revision,
    _st_redecide_is_the_revert,
    _st_decide_refuses_unconfirmed_family,
    _st_decide_proposed_member_confirms_it,
    _st_show_marks_changed_after_decision,
    _st_summary_stored_for_pair,
    _st_record_refuses_path_in_confirmed_family,
    _st_reject_family_keys_by_content,
    _st_remove_member_of_actual_clears_it,
    _st_twin_follows_only_matching_sha,
    _st_scope_excludes_code_and_archive,
    _st_twin_not_in_scope_itself,
    _st_scope_rejects_escape_paths,
    _st_dedupe_nested_drops_covered_roots,
    _st_partial_scan_deletes_nothing,
    _st_missing_root_is_incomplete,
    _st_missing_needs_two_scans,
    _st_available_when_it_reappears,
    _st_relocated_when_unambiguous,
    _st_two_copies_are_duplicates_not_move,
    _st_changed_during_walk_not_absent,
    _st_cache_reuses_unchanged_hash,
    _st_relocation_needs_new_path,
    _st_no_usable_cache_never_relocates,
    _st_two_absent_members_share_sha_not_relocated,
    _st_default_roots_raises_not_silent,
    _st_default_roots_rejects_out_of_root_candidate,
    _st_scan_catches_root_resolution_failure,
    _st_empty_root_set_is_incomplete,
    _st_out_of_scope_member_gets_no_event,
    _st_dedupes_members_by_artifact_id,
    _st_symlinked_directory_is_incomplete,
    _st_resolve_roots_dedupes_explicit_nested,
    _st_cache_validates_sha_shape,
    _st_relocation_not_fooled_by_skipped_file,
    _st_relocation_not_fooled_by_incomplete_pass,
    _st_relocation_not_fooled_by_failed_cache_write,
    _st_relocation_requires_new_ctime,
    _st_absent_member_followed_late,
    _st_late_follow_refuses_long_standing_copy,
    _st_symlinked_parent_of_explicit_root_refused,
    _st_default_roots_refuses_resolved_symlink_swap,
    _st_out_of_scope_not_mutated_across_calls,
    _st_scan_catches_runtime_error_during_root_resolution,
    _st_relocation_not_fooled_by_resaved_skipped_file,
    _st_malformed_cache_entry_rehashed,
    _st_history_deleted_when_ledger_matches,
    _st_reused_history_path_not_deleted,
    _st_proposed_member_held,
    _st_partial_scan_plans_no_deletes,
    _st_guard_blocks_mass_delete,
    _st_header_change_triggers_reupload,
    _st_twin_ids_follow_original,
    _st_delete_404_counts_as_done,
    _st_corrupt_registry_blocks_deletions,
    _st_out_of_scope_member_never_deleted,
    _st_registry_file_never_uploaded,
    _st_generation_change_replans_then_skips,
    _st_deletes_off_by_default,
    _st_lock_not_held_during_delete,
    _st_generation_bump_between_deletions_stops,
    _st_other_transport_error_is_recorded,
    _st_missing_actual_holds_history,
    _st_reused_history_path_new_content_uploaded,
    _st_restore_requeues,
    _st_restore_refuses_bad_path,
    _st_restore_writes_canonical_path,
    _st_restore_ledger_matches_hindsight_sync,
    _st_pending_count_counts_families,
    _st_pending_count_absent_registry_is_zero,
    _st_pending_count_corrupt_registry_exits_nonzero,
    _st_v1_v2_proposed,
    _st_monthly_reports_not_proposed,
    _st_rejected_pair_not_reproposed_after_rename,
    _st_rerun_is_idempotent,
    _st_identical_bytes_reported_as_duplicates,
    _st_representation_not_proposed,
    _st_rejection_not_bypassed_by_third_file,
    _st_new_file_joins_confirmed_family_as_member_proposed,
    _st_possible_move_proposed_to_family,
    _st_shared_template_weighs_little,
    _st_no_text_falls_back_to_name,
    _st_dry_run_writes_nothing,
    _st_empty_generic_names_in_siblings_not_chained,
    _st_sparse_group_refused_dense_kept,
    _st_rejected_family_does_not_freeze_its_files,
    _st_duplicate_family_id_refused,
    _st_vanished_file_noted_not_crash,
    _st_scan_batch_refused_whole,
    _st_office_owner_files_out_of_scope,
    _st_month_word_alone_is_not_a_period,
    _st_shrunk_rejected_family_pair_not_reproposed,
    _st_day_stamps_are_save_dates_periods_are_not,
    _st_alternatives_and_sources_never_versions,
    _st_revision_line_across_folders,
    _st_edited_former_actual_removed_after_new_decision,
    _st_registry_event_missing_key_is_corrupt,
    _st_registry_non_object_line_is_corrupt,
    _st_corrupt_registry_push_sends_non_family_files,
    _st_reject_refuses_confirmed_family,
    _st_reject_paths_refuses_confirmed_members,
    _st_path_in_two_confirmed_families_is_corrupt,
    _st_plan_never_deletes_what_it_uploads_or_holds,
    _st_decide_requires_expect_sha,
    _st_show_prints_full_sha_and_decide_command,
    _st_caches_under_runtime_cache_with_pid_tmp,
    _st_push_prints_scan_errors,
    _st_push_empty_registry_not_reported_as_scan_error,
    _st_locked_timeout_gives_up,
    _st_push_skips_registry_when_lock_busy,
    _st_record_stores_canonical_relative_path,
    _st_restore_refuses_history_and_held,
    _st_commands_resolve_inside_lock,
    _st_prime_check_spawns_venv_python,
    _st_source_files_never_change,
    _st_relocated_history_changes_header,
    _st_rollback_after_membership_change,
    _st_concurrent_decide_and_scan,
    _st_merge_folds_and_dismisses,
    _st_merge_refuses_confirmed_source,
    _st_dismiss_only_single_member_unconfirmed,
    _st_pending_skips_rejected_family,
    _st_archived_member_scanned_never_proposed,
    _st_archived_history_deleted_when_ledger_matches,
    _st_txt_actual_gets_header,
    _st_daily_cap_counts_family_deletions_only,
    _st_push_detect_proposes_before_planning,
    _st_timer_template_detects_and_never_deletes,
]


def cmd_self_test(_args) -> int:
    failed = 0
    for fn in SELF_TESTS:
        with tempfile.TemporaryDirectory(dir=data_write_dir("runtime:scratch/tmp")) as d:
            try:
                fn(Path(d))
                print(f"ok   {fn.__name__}")
            except Exception as exc:  # report every scenario, then fail the run
                failed += 1
                print(f"FAIL {fn.__name__}: {exc!r}")
    print(f"{len(SELF_TESTS) - failed}/{len(SELF_TESTS)} passed")
    return 1 if failed else 0


# ============================================================
# argparse wiring
# ============================================================


def _add_root_registry(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--root", default=None,
                     help="the scope root paths are relative to (default: get_data_root())")
    sp.add_argument("--registry", default=None,
                     help="the registry file (default: version_registry.registry_path())")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("self-test").set_defaults(fn=cmd_self_test)

    sp = sub.add_parser("record")
    sp.add_argument("from_path")
    sp.add_argument("to_path")
    _add_root_registry(sp)
    sp.set_defaults(fn=cmd_record)

    sp = sub.add_parser("pending")
    sp.add_argument("--count", action="store_true")
    _add_root_registry(sp)
    sp.set_defaults(fn=cmd_pending)

    sp = sub.add_parser("show")
    sp.add_argument("family")
    _add_root_registry(sp)
    sp.set_defaults(fn=cmd_show)

    sp = sub.add_parser("confirm")
    sp.add_argument("family")
    _add_root_registry(sp)
    sp.set_defaults(fn=cmd_confirm)

    sp = sub.add_parser("decide")
    sp.add_argument("family")
    sp.add_argument("number")
    sp.add_argument("--expect-sha", default=None)
    _add_root_registry(sp)
    sp.set_defaults(fn=cmd_decide)

    sp = sub.add_parser("reject")
    sp.add_argument("targets", nargs="+")
    _add_root_registry(sp)
    sp.set_defaults(fn=cmd_reject)

    sp = sub.add_parser("remove-member")
    sp.add_argument("family")
    sp.add_argument("number")
    _add_root_registry(sp)
    sp.set_defaults(fn=cmd_remove_member)

    sp = sub.add_parser("merge")
    sp.add_argument("into")
    sp.add_argument("source", metavar="from")
    _add_root_registry(sp)
    sp.set_defaults(fn=cmd_merge)

    sp = sub.add_parser("dismiss")
    sp.add_argument("family")
    sp.add_argument("--reason", default=None)
    _add_root_registry(sp)
    sp.set_defaults(fn=cmd_dismiss)

    sp = sub.add_parser("summary")
    sp.add_argument("family")
    sp.add_argument("from_n")
    sp.add_argument("to_n")
    sp.add_argument("text")
    _add_root_registry(sp)
    sp.set_defaults(fn=cmd_summary)

    sp = sub.add_parser("scan")
    sp.add_argument("--dry-run", action="store_true",
                    help="print the proposals; write no registry event and no cache")
    sp.add_argument("--scan-cache", default=None,
                    help="the scan cache (default: doc_scan.default_cache_path())")
    sp.add_argument("--detector-cache", default=None,
                    help="the shingle cache (default: doc_detector.default_cache_path())")
    sp.add_argument("--scope-root", action="append", default=None,
                    help="a root-relative scope root; repeatable (default: the real scope)")
    _add_root_registry(sp)
    sp.set_defaults(fn=cmd_scan)

    sp = sub.add_parser("restore")
    sp.add_argument("path")
    _add_root_registry(sp)
    sp.add_argument("--ledger", default=None,
                     help="the hindsight-sync ledger file "
                          "(default: runtime:hindsight-sync/state/ledger.jsonl)")
    sp.set_defaults(fn=cmd_restore)

    try:
        args = p.parse_args(argv)
    except SystemExit as exc:
        # argparse calls parser.exit() on a bad invocation or --help/-h; both
        # raise SystemExit rather than returning. A bare `sys.exit()` (no args
        # given to the constructor) carries `code=None`, which argparse never
        # does itself but which nothing here should crash on: `None` means a
        # clean exit, same as a real `0`. Anything else that is not an int
        # (rare, but SystemExit accepts arbitrary objects) is reported as a
        # failure rather than propagated, so the self-test harness never
        # aborts the whole run over one malformed invocation.
        code = exc.code
        return 0 if code is None else (code if isinstance(code, int) else 1)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
