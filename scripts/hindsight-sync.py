#!/usr/bin/env python3
"""Send the data repository's text into a Hindsight memory bank, and keep it current.

    hindsight-sync.py plan [--wave core|rest|all] [--max-bytes N]    # what would be sent, and what is skipped
    hindsight-sync.py push --wave core --limit 200 [--sample] [--batch 10] [--dry-run] [--detect] [--allow-deletes]
    hindsight-sync.py push --path DIR_OR_FILE [--path ...] [--paths FILE] [--dry-run]  # named paths, ahead of the queue
    hindsight-sync.py status [--requeue-failed] >> ledger.jsonl      # the ledger against the server's operations
    hindsight-sync.py cleanup plan [--prefix P] [--save FILE]       # bank documents with no source file; deletes nothing
    hindsight-sync.py cleanup apply --plan FILE --numbers 1-20 [--limit 50] [--dry-run] >> ledger.jsonl
    hindsight-sync.py cleanup apply --prefix P [--limit 50] [--dry-run] >> ledger.jsonl
    hindsight-sync.py blocked list                                   # deletions the mass-delete guard blocked, and their confirm token
    hindsight-sync.py blocked delete --confirm N:H [--dry-run] >> ledger.jsonl
    hindsight-sync.py capture-check [--hours 24] [--settle-min 15]  # every interactive session reached the bank
    hindsight-sync.py self-test                                      # cleanup, mirror and tag scenarios, invented bank, no network

Each file the data repository tracks (or does not ignore) becomes one Hindsight document
whose `document_id` is its repository path, so a re-send replaces it; the ledger
(`runtime:hindsight-sync/state`) decides what is sent again. Every full `push` also MIRRORS:
it deletes, in portions, the documents of files that left the repository, never one still in
flight and never an id this script did not send. `cleanup` reaches the rest only on an
explicit command. `--dry-run` sends, deletes and writes nothing.

Conversations are the Hindsight plugin's (`conversation:<session_id>`, retained on Stop);
older `conversation/<session>/<time>` documents from a removed command are still in the bank.

The full operating manual (waves, document dates, version families, mirror, moves, failed
extractions, capture check, cleanup) is `hindsight-sync-manual.md` in the data repository
under registry name `reference-docs`.

Settings from the gitignored `.env`: `HINDSIGHT_SYNC_API_URL` (default
http://127.0.0.1:8888, the tunnel), `HINDSIGHT_SYNC_BANK` (default `main`),
`HINDSIGHT_SYNC_KEY_FILE` (default ~/.config/hindsight/secrets/hindsight-api-key).
"""
from __future__ import annotations

import argparse
import contextlib
import functools
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils import doc_detector, doc_scan, doc_scope, memory_plan  # noqa: E402
from scripts.utils import version_registry as vr  # noqa: E402
from scripts.utils.api import load_api_key  # noqa: E402
from scripts.utils.data_path import DataPathError, UnknownDataPath, data_path, data_write_dir  # noqa: E402
from scripts.utils.excluded_dirs import in_work_dir  # noqa: E402
from scripts.utils.markdown import frontmatter_date, parse_frontmatter  # noqa: E402
from scripts.utils.paths import get_data_root  # noqa: E402

TEXT_EXT = {e.lstrip(".") for e in memory_plan.SENT_AS_ITSELF}  # one set, shared with the memory plan
# The first wave ("core") is the records the workspace reasons from, named one path
# prefix per line in `wave-core.txt` beside the ledger. The list lives in the data
# repository, not here: this engine names no data address. With no such file every
# file belongs to `rest` and `all` is simply alphabetical.
# One Hindsight document holds at most `--max-bytes`; a longer file is sent as parts of
# that size, cut at line ends, with `document_id` `<path>#part-NN`. A file whose text is
# over `--max-file-bytes` is not sent at all and is listed by `plan`.
# 200 KB, not more: the server cancels a retain after 3,600 s, and MEASURED 2026-09-30
# every document sent over 300 KB (3 of 1,200: 315 KB, 499 KB and the plugin's gitlog)
# was cancelled at that limit while the p99 document, 103 KB, was not.
DEFAULT_MAX_BYTES = 200_000
DEFAULT_MAX_FILE_BYTES = 8_000_000
#: How long one pass waits for the version registry's lock before it gives the
#: registry part of the pass up (the timer must never hang behind `decide`).
REGISTRY_LOCK_TIMEOUT = 30.0


def _setting(name: str, default: str) -> str:
    return load_api_key(name, required=False) or default


def _ledger(name: str = "ledger.jsonl") -> Path:
    return data_path("runtime:hindsight-sync/state") / name


def registry_rels(root: Path) -> set[str]:
    """The version registry, its lock and its torn-tail sidecar, relative to `root`.
    None of them is ever sent: they are this machine's decisions, not documents."""
    try:
        reg = vr.registry_path()
    except DataPathError as exc:
        print(f"version registry address unresolved ({exc}); nothing to exclude", file=sys.stderr)
        return set()
    out = set()
    for f in (reg, reg.with_suffix(".lock"), reg.with_name(reg.name + ".torn")):
        try:
            out.add(PurePosixPath(f.resolve().relative_to(root.resolve())).as_posix())
        except ValueError:
            print(f"version registry file {f} is outside the data root; nothing to exclude", file=sys.stderr)
    return out


def tracked(root: Path) -> list[str]:
    """Every git-tracked path of the data repository, unfiltered."""
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True).stdout
    return [p for p in (raw.decode("utf-8", "surrogateescape") for raw in out.split(b"\0")) if p]


def untracked(root: Path) -> list[str]:
    """Every file of the data repository git does not track and does not ignore (HOS-200):
    a new record is memory before anyone commits it. What `.gitignore` excludes stays out."""
    out = subprocess.run(["git", "-C", str(root), "ls-files", "--others", "--exclude-standard", "-z"],
                         capture_output=True, check=True).stdout
    return [p for p in (raw.decode("utf-8", "surrogateescape") for raw in out.split(b"\0")) if p]


def listed(root: Path) -> list[str]:
    """The files a pass may send: tracked, and untracked but not ignored, in path order."""
    return sorted(set(tracked(root)) | set(untracked(root)))


#: Operator decision, 2026-10-03 (HOS-195): the working material beside a document goes into a `_work/`
#: folder next to it ("yes, _work/ beside the document", translated), and `_work/` stays out of memory the
#: way archives do. Unlike the archive rule, no `wave-admit.txt` prefix lifts it, and a
#: document already sent from such a folder leaves the bank through the mirror step.
WORK_REASON = "inside a _work/ folder (working material beside a document is not memory)"


#: Operator decision, 2026-10-05, on the local model's records: "Keep all of its records on disk only,
#: remove them from Hindsight." (translated) A memory record whose frontmatter names one of these writers
#: (top level or under `metadata:`) is never sent, and the mirror takes out what was.
MACHINE_WRITERS = frozenset({"cognee-distil", "memory-distil"})
MACHINE_REASON = "a machine-written memory record (kept on disk only, operator decision 2026-10-05)"


def _auto_memory_prefix(root: Path) -> str | None:
    """The memory records' directory relative to `root`, with a trailing slash, or None
    when the address does not resolve inside it (then every `.md` file is read)."""
    try:
        return data_path("auto-memory").resolve().relative_to(root.resolve()).as_posix().rstrip("/") + "/"
    except (DataPathError, ValueError) as exc:
        print(f"memory records address unresolved under the data root ({exc}); "
              "every .md file's frontmatter is read for its writer", file=sys.stderr)
        return None


def machine_written(root: Path, p: str, prefix: str | None) -> bool:
    """True for a memory record under `prefix` (every `.md` when None) whose frontmatter
    `written_by`, top level or under `metadata:`, is one of `MACHINE_WRITERS`. Only the
    head of the file is read; a file that cannot be read is not called machine-written."""
    if not p.endswith(".md") or (prefix is not None and not p.startswith(prefix)):
        return False
    try:
        with (root / p).open("rb") as fh:
            head = fh.read(DATE_HEAD_BYTES).decode("utf-8", "replace")
    except OSError:
        return False
    fm, _ = parse_frontmatter(head)
    meta = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
    return bool({str(fm.get("written_by")), str(meta.get("written_by"))} & MACHINE_WRITERS)


@functools.lru_cache(maxsize=4)
def store_prefixes(root: str | None = None) -> tuple[str, ...]:
    """The runtime store's root-relative prefixes: the engine default's and `root`'s registry's.

    Both, so a store the registry has moved (HOS-241, nested in a subject root) is never sent
    and neither is anything still at the default address while the move is under way.
    A registry that cannot be read keeps the default only, and says so.
    """
    from scripts.utils.paths import DataRootError
    from scripts.utils.runtime_dir import RuntimeDirError, default_store_dirname, store_dirname

    names = [default_store_dirname()]
    try:
        names.append(store_dirname(Path(root) if root is not None else get_data_root()))
    except (DataRootError, RuntimeDirError) as exc:
        print(f"[hindsight-sync] the registry's runtime store name is unreadable ({exc}); "
              f"excluding the default `{names[0]}/` only", file=sys.stderr)
    return tuple(dict.fromkeys(f"{name}/" for name in names))


def excluded_why(p: str, exclude: set[str], root: Path | None = None, prefix: str | None = None) -> str | None:
    """Why `candidates()` leaves a listed path out, or None when it keeps it. The one
    place these filters are written, so a named path is refused for the same reason.
    With `root`, a machine-written memory record under `prefix` is read and left out too."""
    store = next((s for s in store_prefixes(str(root) if root is not None else None)
                  if p.startswith(s)), None)
    if store is not None:
        return f"under {store} (the runtime store: caches and indexes)"
    if in_work_dir(p):
        return WORK_REASON
    if "archive" in p.lower() and not (admit_prefixes() and p.startswith(admit_prefixes())):
        return "its path reads as an archive (archives are not sent)"
    if p in exclude:
        return "the version registry or its lock (never sent)"
    name = p.rsplit("/", 1)[-1]
    if "." not in name or name.rsplit(".", 1)[1].lower() not in TEXT_EXT:
        return "not a text type (" + ", ".join(sorted(TEXT_EXT)) + ")"
    if root is not None and machine_written(root, p, prefix):
        return MACHINE_REASON
    return None


def candidates(root: Path, exclude: set[str] | None = None, files: list[str] | None = None) -> list[str]:
    exclude = registry_rels(root) if exclude is None else exclude
    prefix = _auto_memory_prefix(root)
    return sorted(p for p in (listed(root) if files is None else files)
                  if excluded_why(p, exclude, root, prefix) is None)


#: Each wave list's registry group (HOS-243, M051). The script resolves the group's names
#: itself on every pass, so moving a directory is an edit of the registry and nothing
#: else: the generated block `generate-data-files.py` writes into the file is a copy for
#: the reader, and a pass no longer depends on anyone having regenerated it after a move.
#: The hand-written lines of the file still count, beside the group's.
WAVE_GROUPS = {
    "wave-core.txt": "hindsight-wave-core",
    "wave-skip.txt": "hindsight-wave-skip",
    "wave-admit.txt": "hindsight-wave-admit",
    "wave-last.txt": "hindsight-wave-last",
}


@functools.lru_cache(maxsize=None)
def group_prefixes(group: str, root: str) -> tuple[str, ...]:
    """The root-relative prefixes of one registry group's names: `rel/` for a directory,
    `rel` for a file. A tree whose registry defines no such group has none; a name that
    does not resolve is said on stderr and left out (`data_homes.resolve_all`). Cached per
    process: one pass asks this for every listed path, and the registry does not move
    under a running pass."""
    from scripts.utils.data_homes import homes

    base = Path(root)
    try:
        addresses = homes(group, data_root=base, existing=False)
    except UnknownDataPath:
        return ()
    except DataPathError as exc:
        print(f"[hindsight-sync] registry group {group!r} unreadable ({exc}); its prefixes are not applied",
              file=sys.stderr)
        return ()
    out: list[str] = []
    for a in addresses:
        try:
            rel = PurePosixPath(a.resolve().relative_to(base.resolve()).as_posix())
        except ValueError:
            print(f"[hindsight-sync] {a} of group {group!r} is outside the data root; not applied",
                  file=sys.stderr)
            continue
        out.append(f"{rel}/" if a.is_dir() or (not a.exists() and not rel.suffix) else str(rel))
    return tuple(dict.fromkeys(out))


def _prefixes(name: str) -> tuple[str, ...]:
    """The hand-written lines of the wave list `name` beside the ledger, then its registry
    group's prefixes (`WAVE_GROUPS`), duplicates removed."""
    f = _ledger(name)
    hand = (tuple(line.strip() for line in f.read_text(encoding="utf-8").splitlines()
                  if line.strip() and not line.startswith("#")) if f.exists() else ())
    group = WAVE_GROUPS.get(name)
    named = group_prefixes(group, str(get_data_root())) if group else ()
    return tuple(dict.fromkeys(hand + named))


def core_prefixes() -> tuple[str, ...]:
    return _prefixes("wave-core.txt")


def skip_prefixes() -> tuple[str, ...]:
    """Path prefixes never sent, one per line in `wave-skip.txt` beside the ledger: the
    operator's own exclusions, kept in the data repository like the core list."""
    return _prefixes("wave-skip.txt")


def admit_prefixes() -> tuple[str, ...]:
    """Path prefixes sent although they read as an archive, one per line in `wave-admit.txt`
    beside the ledger. Only the archive rule is lifted; every other filter still applies.
    Operator decision, 2026-10-03 (HOS-193), on what to admit: "LinkedIn posts, closed threads,
    approved assignments" (translated); handoffs stay out, and his rule of 2026-09-30, "do not
    load archives for now" (translated), holds for everything else. The list lives in the data repository, not here,
    because these are data addresses and this engine names none."""
    return _prefixes("wave-admit.txt")


def last_prefixes() -> tuple[str, ...]:
    """Path prefixes sent only after everything else, one per line in `wave-last.txt`
    beside the ledger. A run takes the first `--limit` changed files in order, so no file
    under these prefixes goes while any other file is still waiting."""
    return _prefixes("wave-last.txt")


# An image embedded in markdown as a data URI is base64, not text: a MARP deck carried
# 6.5 MB of it around a few pages of prose (MEASURED 2026-09-30). The payload is replaced
# before sending, and the size limit is applied to what is actually sent.
DATA_URI = re.compile(r"data:[\w.+/-]+;base64,[A-Za-z0-9+/=]+")


def sendable_text(f: Path) -> str:
    return DATA_URI.sub("data:(embedded file omitted)", f.read_text(encoding="utf-8", errors="replace"))


def sendable_size(f: Path, max_bytes: int) -> int:
    """The file's size, or, when that is over the limit, the size of what would be sent."""
    size = f.stat().st_size
    return size if size <= max_bytes else len(sendable_text(f).encode("utf-8"))


def in_wave(path: str, wave: str) -> bool:
    core = bool(core_prefixes()) and path.startswith(core_prefixes())
    return wave == "all" or (wave == "core") == core


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def trivial_content(path: Path) -> bool:
    """True when the file is empty or whitespace only (a byte-order mark counted as
    whitespace). Such content is shared by unrelated files, so it proves no move."""
    return not sendable_text(path).replace("\ufeff", "").strip()


def _ledger_meta(path: Path) -> dict:
    """What a ledger line records about a file, taken before its text is read for sending."""
    st = path.stat()
    return {"sha256": sha256(path), "bytes": st.st_size, "mtime": int(st.st_mtime), "ctime_ns": st.st_ctime_ns}


def _stat_unchanged(rec: dict, st) -> bool:
    """True when the file's stat proves it untouched since the send `rec` records, so its
    bytes need not be read. Size and whole-second mtime are not that proof: a writer that
    keeps a length and puts the old mtime back (`memory_touch.py` bumping `access_count`)
    leaves both equal over new bytes. MEASURED 2026-10-03 on the live ledger: every sent
    file whose bytes differed from the ledger's under an equal size and mtime was a memory
    record, and every one had a ctime later than its send (counts in the private record). ctime cannot be set back from user space,
    so it is the proof: equal to the recorded `ctime_ns` on a line that carries one, or
    no later than the send on an older line. The trade: a file whose inode changed with
    its bytes the same (a chmod, a checkout rewriting it, a rename) is hashed on every
    pass until it is next sent."""
    if rec.get("bytes") != st.st_size or rec.get("mtime") != int(st.st_mtime):
        return False
    if "ctime_ns" in rec:
        return rec["ctime_ns"] == st.st_ctime_ns
    try:
        return st.st_ctime <= datetime.fromisoformat(rec["sent"]).timestamp()
    except (KeyError, TypeError, ValueError):  # no usable send time: prove nothing, read the bytes
        return False


def changed(root: Path, path: str, seen: dict[str, dict]) -> bool:
    """New, or different bytes from the last send. A stat that proves the file untouched
    since then (`_stat_unchanged`) skips the hash, so a run over thousands of unchanged
    files reads none of them."""
    rec = seen.get(path)
    if rec is None or rec.get("requeue"):
        return True
    if _stat_unchanged(rec, (root / path).stat()):
        return False
    return rec.get("sha256") != sha256(root / path)


# The date a document is sent with (HOS-191). Until 2026-10-03 it was the file's mtime,
# and MEASURED that day most of the bank carried the day the tree was moved rather than
# the date of their content (counts in the private record). The content's own date comes first, the path's next, the mtime last.
DATE_HEAD_BYTES = 16_384  # the date sources sit at the top of a file; only this much is read
UPDATE_KEYS = ("updated", "last_updated")
CREATE_KEYS = ("date", "created")
LAST_UPDATED_LINE = re.compile(
    r"^[ \t>*_#-]*last[ _]updated[*_]*[ \t]*:[*_]*[ \t]*[\"']?(\d{4}-\d{2}-\d{2})(?![\d])", re.I | re.M)
DATE_IN_NAME = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")


def _a_date(value, latest) -> datetime | None:
    """`value` as midnight UTC of its day, or None when it is unparseable or after `latest`.
    Never guessed: anything `frontmatter_date` refuses is no date."""
    try:
        d = frontmatter_date(value)
    except ValueError:
        return None
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc) if d <= latest else None


def document_date(f: Path, path: str, today=None) -> tuple[datetime, str]:
    """The date a document is sent with, and where it came from. In order: frontmatter
    `updated` / `last_updated`, then the harness memory's `metadata.modified`, then
    frontmatter `date` / `created` (a `.md` file only); a `Last Updated:` line (any text
    type, which also reads a YAML `last_updated:` key); a YYYY-MM-DD in the file name; in
    the nearest directory name that has one; the mtime. A date that does not parse, or one
    after tomorrow in UTC (a day of grace for zones east of UTC), is skipped, never
    guessed. Only the first `DATE_HEAD_BYTES` are read."""
    latest = (today or datetime.now(timezone.utc).date()) + timedelta(days=1)
    with f.open("rb") as fh:
        head = fh.read(DATE_HEAD_BYTES).decode("utf-8", "replace")
    body = head
    if path.endswith(".md"):
        fm, body = parse_frontmatter(head)
        meta = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
        for src, value in [(k, fm.get(k)) for k in UPDATE_KEYS] + [("metadata.modified", meta.get("modified"))] \
                + [(k, fm.get(k)) for k in CREATE_KEYS]:
            if value is not None and (when := _a_date(value, latest)):
                return when, f"frontmatter {src}"
    for m in LAST_UPDATED_LINE.finditer(body):
        if when := _a_date(m.group(1), latest):
            return when, "Last Updated line"
    *dirs, name = path.split("/")
    for where, part in [("file name", name)] + [("directory name", d) for d in reversed(dirs)]:
        for m in DATE_IN_NAME.finditer(part):
            if when := _a_date(m.group(1), latest):
                return when, where
    return datetime.fromtimestamp(f.stat().st_mtime, timezone.utc), "mtime"


def sent_day(rec: dict) -> str | None:
    """The UTC day a ledger line's document was sent with: its `ts` (recorded since
    HOS-191), or, on an older line, the day of its `mtime`, which is what `items_for`
    then sent (`fromtimestamp` of the float and of its whole second fall on one day)."""
    if rec.get("ts"):
        return rec["ts"][:10]
    if isinstance(rec.get("mtime"), int):
        return datetime.fromtimestamp(rec["mtime"], timezone.utc).date().isoformat()
    return None


def date_moved(root: Path, path: str, rec: dict | None) -> bool:
    """The document's date now differs from the day it was last sent with. A line with
    `ts` whose stat proves the file untouched is not read: the date is a function of the
    bytes, the path and the mtime. An older line has its file's head read every pass
    until it is next sent, which is the price of re-dating without a full re-send."""
    if not rec or "sha256" not in rec:
        return False  # never sent, or deleted since: `changed()` decides
    f = root / path
    if rec.get("ts") and _stat_unchanged(rec, f.stat()):
        return False
    day = sent_day(rec)
    return day is not None and document_date(f, path)[0].date().isoformat() != day


def needs_send(root: Path, path: str, seen: dict[str, dict], fps: dict[str, str]) -> bool:
    """Changed bytes, a header fingerprint unlike the one last sent, or a document date
    unlike the one last sent. A file outside the families has no fingerprint on either
    side, so for it the second never fires."""
    rec = seen.get(path)
    return (changed(root, path, seen) or (rec or {}).get("header_fp") != fps.get(path)
            or date_moved(root, path, rec))


def pending(api: str, bank: str, key: str) -> int:
    return int(_api("GET", f"{api}/v1/default/banks/{bank}/operations?status=pending&limit=1", key).get("total", 0))


def ledger_lines() -> list[dict]:
    led = _ledger()
    if not led.exists():
        return []
    return [json.loads(line) for line in led.read_text(encoding="utf-8").splitlines() if line.strip()]


def last_sent() -> dict[str, dict]:
    seen: dict[str, dict] = {}
    led = _ledger()
    if led.exists():
        for line in led.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            # the last line per path wins, including a delete_intent/deleted line; changed() then
            # finds no sha256 or bytes on it and sends the file again, which is what a deleted
            # document needs if it is ever uploadable once more
            seen[rec["path"]] = rec
    return seen


def select(root: Path, wave: str, max_file_bytes: int, max_bytes: int = DEFAULT_MAX_BYTES) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    send, too_big = [], []
    skip = skip_prefixes()
    gone = 0
    known = set(tracked(root))
    new = set(untracked(root)) - known
    for p in candidates(root, files=sorted(known | new)):
        if not in_wave(p, wave) or (skip and p.startswith(skip)):
            continue
        try:
            size = sendable_size(root / p, max_bytes)
        except FileNotFoundError:  # listed but off the disk: an edit or a removal not yet committed
            gone += 1
            continue
        (too_big if size > max_file_bytes else send).append((p, size))
    if gone:
        print(f"{gone} listed file(s) not on disk, not sent this pass", file=sys.stderr)
    print(f"{sum(1 for p, _ in send if p in new)} untracked file(s) git does not ignore are in this plan",
          file=sys.stderr)
    core, last = core_prefixes(), last_prefixes()
    # the core wave first, the `wave-last.txt` prefixes after everything else
    send.sort(key=lambda ps: (bool(last) and ps[0].startswith(last), not (core and ps[0].startswith(core)), ps[0]))
    return send, too_big


def named_entries(args, root: Path) -> tuple[list[str], list[tuple[str, str]]] | None:
    """The data-relative paths given by `--path` and `--paths FILE`, in the order given, and
    the entries that are not a data-relative path, with the reason. None when neither
    option was given. An absolute path inside the data root is made relative to it."""
    raw = list(args.path or [])
    if args.paths:
        raw += [line.strip() for line in Path(args.paths).expanduser().read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.strip().startswith("#")]
    if not args.path and not args.paths:
        return None
    entries, bad = [], []
    for e in raw:
        e = e.strip()
        if PurePosixPath(e).is_absolute():
            try:
                e = Path(e).resolve().relative_to(root.resolve()).as_posix()
            except ValueError:
                bad.append((e, "an absolute path outside the data repository"))
                continue
        rel = PurePosixPath(e.removeprefix("./")).as_posix().rstrip("/")
        if rel in ("", ".") or ".." in PurePosixPath(rel).parts:
            bad.append((e, "not a path inside the data repository"))
            continue
        if rel not in entries:
            entries.append(rel)
    return entries, bad


def pick_named(root: Path, entries: list[str], send: list[tuple[str, int]], too_big: list[tuple[str, int]],
               max_file_bytes: int) -> tuple[set[str], list[tuple[str, str]], list[str]]:
    """The files of the normal plan under the named entries (a directory names every file
    under it), each refused file with the reason the normal plan refuses it, and the
    entries that match no tracked file at all."""
    under = lambda p, e: p == e or p.startswith(e + "/")  # noqa: E731
    sendable = {p for p, _ in send}
    big = dict(too_big)
    skip, exclude, prefix = skip_prefixes(), registry_rels(root), _auto_memory_prefix(root)
    files = listed(root)
    picked, refused, unmatched, seen = set(), [], [], set()
    for e in entries:
        hits = [p for p in files if under(p, e)]
        if not hits:
            unmatched.append(e)
            continue
        for p in hits:
            if p in seen:
                continue
            seen.add(p)
            if p in sendable:
                picked.add(p)
                continue
            why = excluded_why(p, exclude, root, prefix)
            if why and why.startswith("not a text type"):
                twin = p.rsplit(".", 1)[0] + "-extract.md"
                why += "; its -extract.md companion is sent instead" if twin in files else "; it has no -extract.md companion"
            elif why is None and skip and p.startswith(skip):
                why = "listed in wave-skip.txt"
            elif why is None and p in big:
                why = f"its text is {big[p]} bytes, over --max-file-bytes {max_file_bytes}"
            refused.append((p, why or "not in the plan"))
    return picked, refused, unmatched


def _api(method: str, url: str, key: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"not an http(s) URL: {url}")
    req = urllib.request.Request(url, data=data, method=method,  # noqa: S310 - scheme checked above
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310 - URL comes from the operator's .env
        return json.load(r)


def split_text(text: str, max_bytes: int) -> list[str]:
    """`text` in consecutive parts of at most `max_bytes` UTF-8 bytes, cut at line ends; a
    single line longer than that is cut by characters."""
    parts, cur, size = [], [], 0
    for line in text.splitlines(keepends=True):
        while len(line.encode("utf-8")) > max_bytes:
            cut = max(1, max_bytes // 4)  # at most four bytes per character; never 0, or this never ends
            parts, cur, size = parts + (["".join(cur)] if cur else []) + [line[:cut]], [], 0
            line = line[cut:]
        n = len(line.encode("utf-8"))
        if cur and size + n > max_bytes:
            parts.append("".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += n
    if cur or not parts:
        parts.append("".join(cur))
    return parts


@functools.lru_cache(maxsize=None)
def _memory_area(root: Path) -> str | None:
    """The memory records' directory relative to `root`, without the trailing slash, asked once."""
    prefix = _auto_memory_prefix(root)
    return prefix.rstrip("/") if prefix else None


def area_of(root: Path, path: str) -> str:
    """The `area:` tag's value: the first two path parts, except that a file directly in the
    memory records' directory takes the directory (HOS-230, operator decision 2026-10-10): the first two
    parts there name the single file, and one-document tags fragment observation scopes."""
    mem = _memory_area(root)
    if mem and path.startswith(mem + "/") and "/" not in path[len(mem) + 1:]:
        return mem
    return "/".join(path.split("/")[:2])


# ============================================================
# Domain tags (HOS-230)
# ============================================================

#: Operator decision, 2026-10-10 (HOS-230): every file document carries one `domain:` tag. Its value comes
#: from the label file `memory-domain-labels` (one JSON line per data file: path, domain, by);
#: a file with no label is asked of Kimi K3 (on the named trust list since 2026-09-14) from its
#: path and opening, with the classifier wording HELM used for the bank's labelling pass, and
#: the answer goes to `DOMAIN_NEW_LABELS` under the ledger's directory for HELM to fold into the
#: label file. A file Kimi does not answer for is sent as `domain:unlabelled`; the send never
#: waits on it beyond `DOMAIN_DEADLINE`, and never fails for it.
DOMAINS = ("engine", "business", "private", "mixed")
DOMAIN_UNLABELLED = "unlabelled"
DOMAIN_MODEL = "kimi-for-coding"
DOMAIN_HEAD_CHARS = 1200
DOMAIN_BATCH = 20
DOMAIN_ATTEMPTS = 3
#: Seconds one pass waits for Kimi's answers before sending the rest as unlabelled. The proxy's
#: own worst case is far longer (`proxy_transport.call_model`), and the timer must not hang on it.
DOMAIN_DEADLINE = 300.0
DOMAIN_NEW_LABELS = "domain-labels-new.jsonl"
DOMAIN_INSTR = """You label documents from one person's workspace. The person is <NAME>, founder and CEO of <COMPANY>. They also run their own AI assistant system called HEADING OS.

Give each document exactly one domain:
- engine: about the AI assistant system itself (HEADING OS): its code, scripts, hooks, rules, skills, memory server, tooling, infrastructure, technical plans, audits and sessions that build or run the assistant.
- business: about the company and its work: products, customers, deals, partners, investors, contracts, pricing, the team, marketing, market and competitor research, company documents, business contacts.
- private: about <NAME>'s personal life: home, property, mortgage, family, personal finances and banking, personal contracts, personal health, personal travel, personal education.
- mixed: only when the document is substantially about two of the above and neither dominates.

Decide from the path and the opening text. Answer ONLY a JSON array, one object per document, in the same order: {"i": <number>, "domain": "<engine|business|private|mixed>", "reason": "<at most 12 words>"}."""


def domain_instr() -> str:
    """DOMAIN_INSTR with the operator and the company from operator.yaml (`get_operator()`).

    Filled by replacement, not `str.format`, because the instruction carries a
    JSON example whose braces `format` would read as fields.
    """
    from scripts.utils.operator_identity import get_operator
    who = get_operator()
    return (DOMAIN_INSTR.replace("<NAME>", who["name"])
            .replace("<COMPANY>", who["company"] or "a company"))


def load_domain_labels() -> dict[str, str]:
    """path -> domain, from Kimi's answers not yet folded in and then the label file, which
    wins where both name a path. A missing file is no labels; a bad line is counted and said."""
    labels: dict[str, str] = {}
    for name, where in ((DOMAIN_NEW_LABELS, lambda: _ledger(DOMAIN_NEW_LABELS)),
                        ("memory-domain-labels", lambda: data_path("memory-domain-labels"))):
        try:
            lines = where().read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            continue
        except (DataPathError, OSError) as exc:
            print(f"domain: {name} unreadable ({exc}); its files are asked of Kimi", file=sys.stderr)
            continue
        bad = 0
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if isinstance(rec, dict) and isinstance(rec.get("path"), str) and rec.get("domain") in DOMAINS:
                labels[rec["path"]] = rec["domain"]
            elif line.strip():
                bad += 1
        if bad:
            print(f"domain: {bad} line(s) of {name} carry no path and domain; ignored", file=sys.stderr)
    return labels


def ask_domains(root: Path, paths: list[str]) -> dict[str, str]:
    """Kimi's domain for each of `paths`, asked through the proxy in batches of `DOMAIN_BATCH`
    from the path and the first `DOMAIN_HEAD_CHARS` characters. A path it did not answer for
    is absent; a batch that fails is said on stderr and the rest are still asked."""
    try:
        from scripts.utils.proxy_transport import call_model
    except ImportError as exc:
        print(f"domain: proxy transport unavailable ({exc}); nothing asked of Kimi", file=sys.stderr)
        return {}
    out: dict[str, str] = {}
    for i in range(0, len(paths), DOMAIN_BATCH):
        batch = []
        for p in paths[i: i + DOMAIN_BATCH]:
            try:
                batch.append((p, (root / p).read_text(encoding="utf-8", errors="replace")[:DOMAIN_HEAD_CHARS]))
            except OSError as exc:
                print(f"domain: {p} unreadable for Kimi ({exc})", file=sys.stderr)
        if not batch:
            continue
        prompt = "\n".join([domain_instr(), ""] + [f"### Document {n}\nPath: {p}\nOpening:\n{t}\n"
                                                 for n, (p, t) in enumerate(batch)])
        # up to DOMAIN_ATTEMPTS asks, as HELM's labelling pass did: MEASURED 2026-10-11, the same
        # one-file prompt came back once without a JSON array and, asked again, with one
        for attempt in range(1, DOMAIN_ATTEMPTS + 1):
            answer = ""
            try:
                answer = call_model(DOMAIN_MODEL, prompt, temperature=0.0, max_tokens=4000, timeout=120)
                # asked about one file, Kimi sometimes answers the bare object (MEASURED 2026-10-11)
                body = (answer[answer.index("["): answer.rindex("]") + 1] if "[" in answer
                        else "[" + answer[answer.index("{"): answer.rindex("}") + 1] + "]")
                got = {int(o["i"]): o.get("domain") for o in json.loads(body) if isinstance(o, dict)}
            except (RuntimeError, ValueError, KeyError, TypeError) as exc:
                print(f"domain: Kimi gave no usable answer for {len(batch)} file(s), attempt {attempt} of "
                      f"{DOMAIN_ATTEMPTS} ({type(exc).__name__}: {str(exc)[:200]}; answer {answer[:120]!r})",
                      file=sys.stderr)
                continue
            out.update({p: got[n] for n, (p, _) in enumerate(batch) if got.get(n) in DOMAINS})
            break
    return out


def record_new_labels(got: dict[str, str]) -> None:
    """Append Kimi's answers to `DOMAIN_NEW_LABELS` beside the ledger, never to the label file."""
    by = f"kimi {datetime.now(timezone.utc).date().isoformat()} hindsight-sync"
    try:
        with (data_write_dir("runtime:hindsight-sync/state") / DOMAIN_NEW_LABELS).open("a", encoding="utf-8") as fh:
            for p, d in sorted(got.items()):
                fh.write(json.dumps({"path": p, "domain": d, "by": by}, ensure_ascii=False) + "\n")
    except (DataPathError, OSError) as exc:
        print(f"domain: Kimi's {len(got)} answer(s) not recorded ({exc}); they are asked again next pass",
              file=sys.stderr)


def domains_for(root: Path, paths: list[str], labels: dict[str, str], dry_run: bool) -> dict[str, str]:
    """The domain of every path: its label, else Kimi's answer, else `DOMAIN_UNLABELLED`.
    Kimi is asked in a daemon thread for at most `DOMAIN_DEADLINE` seconds, so a slow or
    unreachable proxy delays the pass by that much and never stops it. Answers are recorded
    unless `dry_run`."""
    ask = sorted({p for p in paths if p not in labels})
    got: dict[str, str] = {}
    if ask:
        box: dict[str, dict[str, str]] = {}
        worker = threading.Thread(target=lambda: box.setdefault("got", ask_domains(root, ask)), daemon=True)
        worker.start()
        worker.join(DOMAIN_DEADLINE)
        if worker.is_alive():
            print(f"domain: Kimi did not finish within {DOMAIN_DEADLINE:.0f} s", file=sys.stderr)
        got = dict(box.get("got") or {})
        if got and not dry_run:
            record_new_labels(got)
        missing = [p for p in ask if p not in got]
        print(f"domain: {len(ask)} file(s) without a label, {len(got)} labelled by Kimi"
              + (" (not recorded: dry run)" if got and dry_run else ""), file=sys.stderr)
        for p in missing:
            print(f"  domain:{DOMAIN_UNLABELLED} {p}", file=sys.stderr)
    return {p: labels.get(p) or got.get(p) or DOMAIN_UNLABELLED for p in paths}


def items_for(root: Path, path: str, max_bytes: int = DEFAULT_MAX_BYTES, header: str = "",
              when: str | None = None, domain: str | None = None) -> list[dict]:
    """The documents one file is sent as. `header` (an actual version's) goes after the
    first line of every part; empty, the items are exactly what they always were.
    `when` is the timestamp to send, normally the `ts` already put in the ledger line;
    without it `document_date` is asked. `domain` adds `domain:<domain>` to every part's
    tags; `push` always passes one (`domains_for`)."""
    f = root / path
    lead = f"{header}\n\n" if header else ""
    area = area_of(root, path)
    # the header's bytes count against the limit, so no part with its header exceeds it
    parts = split_text(sendable_text(f), max(1, max_bytes - len(lead.encode("utf-8"))))
    base = {
        "context": f"HEADING OS workspace file ({area})",
        "timestamp": when or document_date(f, path)[0].isoformat(),
        # Operator decision, 2026-10-03 (HOS-189), "5 - yes" (translated): a file under any `personal/` directory is marked
        # personal; the content is unchanged. A re-retain replaces the tags, so every send adds it.
        "tags": ["workspace", f"area:{area}"] + (["personal"] if "/personal/" in "/" + path else [])
                + ([f"domain:{domain}"] if domain else []),
        "strategy": "document",  # the Coding Agents plugin's document strategy, defined on the bank
    }
    if len(parts) == 1:
        return [{"content": f"Workspace file: {path}\n\n{lead}{parts[0]}", "document_id": path, **base}]
    n = len(parts)
    return [{"content": f"Workspace file: {path} (part {i} of {n})\n\n{lead}{t}",
             "document_id": f"{path}#part-{i:02d}", **base} for i, t in enumerate(parts, 1)]


def cmd_plan(args) -> int:
    root = get_data_root()
    send, too_big = select(root, args.wave, args.max_file_bytes, args.max_bytes)
    seen = last_sent()
    todo = [(p, s) for p, s in send if changed(root, p, seen)]
    print(f"wave {args.wave}: {len(send)} files, {sum(s for _, s in send) / 1e6:.1f} MB; "
          f"{len(todo)} new or changed since the last send")
    print(f"skipped over {args.max_file_bytes} bytes: {len(too_big)}; "
          f"sent in parts (over {args.max_bytes} bytes): {sum(1 for _, s in send if s > args.max_bytes)}")
    for p, s in too_big:
        print(f"  {s / 1e6:6.1f} MB  {p}")
    return 0


def _prev_absent(state: vr.State) -> set[str]:
    """Members the registry already records as unavailable: the registry is the memory of
    the previous pass, so a second complete absence makes them missing."""
    return {m.artifact_id for fam in state.families.values()
            for m in list(fam.members.values()) + list(fam.proposed.values()) if m.state == "unavailable"}


def prepare_memory(root: Path, reg: Path, cache: Path, dry_run: bool,
                   roots=None, scan_empty: bool = False) -> tuple[vr.State | None, doc_scan.ScanResult, str | None]:
    """The registry state to plan from, after this pass's scan, and the scan itself.

    Returns `(None, result, why)` when the registry part of the pass is skipped: the
    registry is corrupt, or its lock stayed busy for `REGISTRY_LOCK_TIMEOUT` seconds.
    Outside a dry run the availability events are appended under the registry lock,
    computed against the state read under that same lock; in a dry run they are only
    applied in memory, and the scan cache is not written. An empty registry is not scanned
    unless `scan_empty` (`--detect`, whose proposals are how a registry stops being empty)."""
    try:
        state = vr.replay(vr.read_events(reg)[0])
    except vr.RegistryCorrupt as exc:
        return None, doc_scan.ScanResult(complete=False, errors=[str(exc)]), f"corrupt: {exc}"
    if not state.families and not scan_empty:
        return state, doc_scan.ScanResult(complete=False), None
    result = doc_scan.scan(root, cache, roots=roots, write_cache=not dry_run,
                           members=doc_scan.confirmed_member_paths(state))
    try:
        if dry_run:
            events = vr.read_events(reg)[0]
            new, _ = doc_scan.availability_events(state, _prev_absent(state), result)
            last = max((e["seq"] for e in events), default=0)
            synth = [{"schema_version": vr.SCHEMA_VERSION, "event_id": f"dry-run-{i}", "seq": last + i,
                      "ts": "", "actor": "dry-run", **e} for i, e in enumerate(new, 1)]
            for e in new:
                print(f"  registry event (not written, dry run): {e['type']} {e['artifact_id']}", file=sys.stderr)
            return vr.replay(events + synth), result, None
        with vr.locked(reg, timeout=REGISTRY_LOCK_TIMEOUT):
            st = vr.replay(vr.read_events(reg)[0])
            new, _ = doc_scan.availability_events(st, _prev_absent(st), result)
            for e in new:
                vr.append(e, path=reg)
                print(f"  registry event: {e['type']} {e['artifact_id']}", file=sys.stderr)
            return vr.replay(vr.read_events(reg)[0]), result, None
    except vr.RegistryCorrupt as exc:
        return None, result, f"corrupt: {exc}"
    except vr.LockTimeout as exc:
        return None, result, f"registry lock busy: {exc}"


def run_detector(root: Path, reg: Path, state: vr.State, result: doc_scan.ScanResult,
                 dry_run: bool) -> vr.State:
    """`--detect`: the detector proposes from this pass's scan, before the plan is made.

    Proposes only (`family_proposed` / `member_proposed`); confirming and deciding stay
    the operator's. An incomplete scan proposes nothing. A dry run prints the proposals
    and applies them in memory only, so its plan is the one a live pass would make; a
    live pass appends them as one batch under the registry lock (`append_proposals`).
    A refused batch or a busy lock leaves the registry as it was and the pass goes on
    with the state it had. Returns the state to plan from."""
    if not result.complete:
        print("detector: scan incomplete, nothing proposed this pass", file=sys.stderr)
        return state
    det_cache = doc_detector.default_cache_path()
    det = doc_detector.detect(state, result, root, cache=doc_detector.load_cache(det_cache))
    for ev in det.events:
        members = ev["members"] if ev["type"] == "family_proposed" else [ev["member"]]
        where = "new family" if ev["type"] == "family_proposed" else f"new member for family {ev['family_id']}"
        print(f"  detector proposes {where}: {' | '.join(m['path'] for m in members)}"
              + (" (not written, dry run)" if dry_run else ""), file=sys.stderr)
    if dry_run:
        events = vr.read_events(reg)[0]
        last = max((e["seq"] for e in events), default=0)
        synth = [{"schema_version": vr.SCHEMA_VERSION, "event_id": f"dry-run-detect-{i}", "seq": last + i,
                  "ts": "", "actor": "dry-run", **e} for i, e in enumerate(det.events, 1)]
        try:
            return vr.replay(events + synth)
        except vr.RegistryCorrupt as exc:
            print(f"detector: the batch would be refused ({exc}); planning without it", file=sys.stderr)
            return state
    doc_detector.save_cache(det_cache, det.cache)
    try:
        with vr.locked(reg, timeout=REGISTRY_LOCK_TIMEOUT):
            written = doc_detector.append_proposals(reg, state, det, result, root)
            print(f"detector: {len(written)} proposal event(s) appended", file=sys.stderr)
            return vr.replay(vr.read_events(reg)[0])
    except vr.RegistryCorrupt as exc:
        print(f"detector: batch refused, nothing appended ({exc})", file=sys.stderr)
    except vr.LockTimeout as exc:
        print(f"detector: registry lock busy ({exc}); nothing proposed this pass", file=sys.stderr)
    return state


def _family_deletions_today() -> int:
    """Family deletions the ledger shows attempted in the last 24 hours."""
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    return memory_plan.family_deletions_since(ledger_lines(), since)


def _could_be_member(rel: str, roots) -> bool:
    """True when `rel` is a tracked document, or a twin of one, under the scan roots."""
    base = rel[: -len("-extract.md")] + ".md" if rel.endswith("-extract.md") else rel
    return doc_scope.in_scope(base, roots=roots)


def delete_document(api: str, bank: str, key: str, doc_id: str) -> str:
    """DELETE one document. "deleted", or "absent" for a 404 (already gone counts as done)."""
    url = f"{api}/v1/default/banks/{bank}/documents/{urllib.parse.quote(doc_id, safe='')}"
    try:
        _api("DELETE", url, key)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(f"  {doc_id}: not in the bank (404), counted as deleted", file=sys.stderr)
            return "absent"
        raise
    except json.JSONDecodeError as exc:
        print(f"  {doc_id}: deleted; the response had no JSON body ({exc})", file=sys.stderr)
    return "deleted"


#: Operation states after which a retain writes nothing more into the bank.
SETTLED = {"completed", "failed"}


class SendCheck:
    """Is the last send of a document still being written by the server?

    A retain is asynchronous: deleting its document before the operation settles lets
    the late retain create it again after the ledger recorded it deleted, and no later
    pass touches it (MEASURED 2026-10-12: operation 5c0e9a28 sent 17:23:43Z, settled
    17:55:26Z; the mirror deleted its documents at 17:39 and 7 came back 17:43-17:46).
    So every delete path asks `why_wait()` first and defers on any answer. Per id, the
    last ledger upload that carried it names its operations; each must be settled, its
    `child_operations` too. A status that cannot be read defers (fail safe). An id the
    ledger never shows sent has no send of this script in flight. One GET per operation
    per pass; a settled status never changes back, and a pending one defers anyway."""

    def __init__(self, api: str, bank: str, key: str, lines: list[dict]):
        self.api, self.bank, self.key = api, bank, key
        self.sends: dict[str, dict] = {}
        for rec in lines:
            path = rec.get("path")
            if path and "sha256" in rec and "requeue" not in rec:
                for doc_id in memory_plan._ids_of_upload(path, rec):
                    self.sends[doc_id] = rec
        self.status: dict[str, str | None] = {}  # operation id -> None when settled, else why not
        self.failed: set[str] = set()  # settled operations of which it or a child failed

    def _op_wait(self, op: str) -> str | None:
        if op not in self.status:
            try:
                got = _api("GET", f"{self.api}/v1/default/banks/{self.bank}/operations/{op}", self.key)
            except urllib.error.HTTPError as exc:
                self.status[op] = f"operation {op} status unreadable (HTTP {exc.code})"
            except (urllib.error.URLError, OSError, ValueError) as exc:
                self.status[op] = f"operation {op} status unreadable ({type(exc).__name__}: {exc})"
            else:
                states = [got.get("status")] + [c.get("status") for c in got.get("child_operations") or []
                                                 if isinstance(c, dict)]
                open_ = [str(s) for s in states if s not in SETTLED]
                if "failed" in states:
                    self.failed.add(op)
                self.status[op] = (f"operation {op} not settled ({', '.join(sorted(set(open_)))})"
                                   if open_ else None)
        return self.status[op]

    def why_wait(self, doc_id: str) -> str | None:
        """Why `doc_id` must not be deleted yet, or None."""
        rec = self.sends.get(doc_id)
        if rec is None:
            return None
        ops = rec.get("operation_ids") or [rec.get("operation_id")]
        if not all(ops):
            return "its last send recorded no operation id; status unknown"
        for op in ops:
            wait = self._op_wait(op)
            if wait:
                return f"its last send is still in flight: {wait}"
        return None

    def why_unproven(self, doc_id: str) -> str | None:
        """Why the last send of `doc_id` does not PROVE its content recallable, or None.
        Settled is enough to delete an id (`why_wait`), never to prove a move: a failed
        extraction settles too and leaves nothing to recall. Proof needs a recorded send
        whose every operation, children included, is `completed`."""
        wait = self.why_wait(doc_id)
        if wait:
            return wait
        rec = self.sends.get(doc_id)
        if rec is None:
            return "no send of it is recorded"
        for op in rec.get("operation_ids") or [rec.get("operation_id")]:
            if op in self.failed:
                return f"its last send failed: operation {op} (or a child) failed"
        return None


def defer_unsettled(p: memory_plan.Plan, check: SendCheck) -> memory_plan.Plan:
    """Take out of `p.delete` every id `check` says must wait, each printed with its
    reason. Run before `cap_daily`, so a deferred id takes no place under the caps."""
    keep = []
    for doc_id in p.delete:
        wait = check.why_wait(doc_id)
        if wait:
            print(f"  defer delete {doc_id}: {wait}; asked again next pass", file=sys.stderr)
        else:
            keep.append(doc_id)
    p.delete = keep
    return p


def _read_state(reg: Path) -> vr.State:
    return vr.replay(vr.read_events(reg)[0])


def run_deletions(plan: memory_plan.Plan, api: str, bank: str, key: str, generation: int,
                  reg: Path, read_state=None) -> int:
    """Delete the planned documents one at a time, never holding the registry lock over a call.

    Per id: under the lock, the registry generation is read and must still be the one the
    plan was made against, and the intent line is written; the lock is released; the DELETE
    runs; the result line is written. A moved generation stops the remaining deletions. Any
    failure writes a `delete_failed` line and stops them too: an id with no `deleted` line is
    asked again next pass, which is safe because a delete is idempotent."""
    read_state = read_state or (lambda: _read_state(reg))
    for doc_id in plan.delete:
        path = plan.delete_path[doc_id]
        try:  # LockTimeout can only come from taking the lock, never from the body
            with vr.locked(reg, timeout=REGISTRY_LOCK_TIMEOUT):
                try:
                    now_gen = read_state().generation
                except vr.RegistryCorrupt as exc:
                    print(f"  registry unreadable before deleting {doc_id} ({exc}); stopping deletions", file=sys.stderr)
                    return 0
                if now_gen != generation:
                    print(f"  registry generation moved {generation} -> {now_gen}; stopping deletions this pass",
                          file=sys.stderr)
                    return 0
                print(json.dumps({"path": path, "delete_intent": doc_id, "generation": generation,
                                  "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}), flush=True)
        except vr.LockTimeout as exc:
            print(f"  {exc}; stopping deletions this pass", file=sys.stderr)
            return 0
        if not delete_recorded(api, bank, key, doc_id, {"path": path, "generation": generation}):
            print("  stopping deletions, asked again next pass", file=sys.stderr)
            return 1
    return 0


def delete_recorded(api: str, bank: str, key: str, doc_id: str, base: dict) -> bool:
    """DELETE one id and print its result ledger line, `base` plus `at` and either
    `deleted` (with `absent` for a 404) or `delete_failed` with the error. The caller
    printed the `delete_intent` line before calling. False on any failure."""
    failure = None
    try:
        outcome = delete_document(api, bank, key, doc_id)
    except urllib.error.HTTPError as exc:
        failure = f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError) as exc:
        failure = f"{exc}; outcome unknown"
    except Exception as exc:  # anything else from the transport: logged, recorded, deletions stop
        failure = f"{type(exc).__name__}: {exc}"
    rec = {**base, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if failure is not None:
        print(json.dumps({**rec, "delete_failed": doc_id, "error": failure[:300]}), flush=True)
        print(f"  delete {doc_id}: {failure}", file=sys.stderr)
        return False
    rec["deleted"] = doc_id
    if outcome == "absent":
        rec["absent"] = True
    print(json.dumps(rec), flush=True)
    print(f"  deleted {doc_id}", file=sys.stderr)
    return True


def deletion_phase(reg: Path, planned_generation: int, plan: memory_plan.Plan, replan,
                   api: str, bank: str, key: str, read_state=None) -> int:
    """Re-check the plan under a short registry lock, then delete one id at a time.

    A registry generation that moved since planning re-plans once through `replan(state)`;
    a second move skips deletions this pass, as does a registry that no longer replays.
    The lock is released before any DELETE (see `run_deletions`)."""
    read_state = read_state or (lambda: _read_state(reg))
    try:  # LockTimeout can only come from taking the lock, never from the body
        with vr.locked(reg, timeout=REGISTRY_LOCK_TIMEOUT):
            try:
                st = read_state()
                if st.generation != planned_generation:
                    print(f"registry generation moved {planned_generation} -> {st.generation} since planning; "
                          "planning again", file=sys.stderr)
                    plan = replan(st)
                    again = read_state()
                    if again.generation != st.generation:
                        print(f"registry generation moved again ({st.generation} -> {again.generation}); "
                              "no deletions this pass", file=sys.stderr)
                        return 0
                    planned_generation = st.generation
            except vr.RegistryCorrupt as exc:
                print(f"registry unreadable at the re-check ({exc}); no deletions this pass", file=sys.stderr)
                return 0
    except vr.LockTimeout as exc:
        print(f"{exc}; no deletions this pass", file=sys.stderr)
        return 0
    if plan.blocked:
        print(f"deletions stopped: {plan.blocked}. Waiting for the operator.", file=sys.stderr)
        return 0
    if not plan.delete:
        return 0
    return run_deletions(plan, api, bank, key, planned_generation, reg, read_state)


def finish_deletions(mplan: memory_plan.Plan, allow: bool, reg: Path, generation: int, replan,
                     api: str, bank: str, key: str, read_state=None) -> int:
    """The deletion step of a live pass. Nothing to delete and nothing blocked: no lock is
    taken at all. Without `--allow-deletes` every planned id is printed as disabled and the
    registry lock is never taken."""
    if not mplan.delete and not mplan.blocked:
        return 0
    if not allow:
        for doc_id in mplan.delete:
            print(f"  delete {doc_id} (disabled: --allow-deletes not given)", file=sys.stderr)
        return 0
    if mplan.blocked:
        return 0  # already printed with the plan; deletions wait for the operator
    return deletion_phase(reg, generation, mplan, replan, api, bank, key, read_state)


def _print_memory_plan(mplan: memory_plan.Plan, allow: bool, dry_run: bool) -> None:
    """Hold, note and blocked lines on every pass; delete lines here only on a dry run
    (a live pass prints them as it deletes, or as disabled)."""
    for s in sorted(mplan.hold):
        print(f"  hold {s}: {mplan.reasons.get(s, 'held')}", file=sys.stderr)
    for s in sorted(mplan.skip - mplan.hold):
        print(f"  hold {s}: history or unavailable, never sent", file=sys.stderr)
    if dry_run:
        for doc_id in mplan.delete:
            print(f"  delete {doc_id} (ledger path {mplan.delete_path[doc_id]})" if allow
                  else f"  delete {doc_id} (disabled: --allow-deletes not given)", file=sys.stderr)
    if mplan.blocked:
        print(f"  blocked: {mplan.blocked}. Waiting for the operator.", file=sys.stderr)
    for note in mplan.notes:
        print(f"  note: {note}", file=sys.stderr)


def family_plan(state: vr.State, led: dict[str, dict], result, root: Path, allow: bool,
                check: SendCheck) -> memory_plan.Plan:
    """The memory plan, with every family deletion whose send is still in flight deferred.
    Asked only when deletions are allowed and not blocked; otherwise none runs anyway."""
    p = memory_plan.plan(state, led, result, memory_plan.uploaded_count(led), root=root)
    return defer_unsettled(p, check) if allow and not p.blocked else p


def cmd_push(args) -> int:
    root = get_data_root()
    api, bank, key = _conn()
    try:
        named = named_entries(args, root)
    except OSError as exc:
        print(f"--paths file unreadable ({exc}); sending nothing", file=sys.stderr)
        return 2
    refused: list[tuple[str, str]] = []
    unmatched: list[str] = []
    if named is None:
        send, _ = select(root, args.wave, args.max_file_bytes, args.max_bytes)
    else:
        # Named paths are chosen from every wave: a wave orders the backlog, it is not a filter.
        send, too_big = select(root, "all", args.max_file_bytes, args.max_bytes)
        entries, refused = named[0], list(named[1])
        picked, refused_files, unmatched = pick_named(root, entries, send, too_big, args.max_file_bytes)
        refused += refused_files
        send = [(p, s) for p, s in send if p in picked]
        print(f"named paths: {len(entries)} entr(y/ies), {len(send)} file(s) the plan would send", file=sys.stderr)
    seen = last_sent()
    if args.max_pending and not args.dry_run and pending(api, bank, key) >= args.max_pending:
        print(f"server queue at or above {args.max_pending} pending operations; sending nothing this run",
              file=sys.stderr)
        return 0
    # The memory plan: the registry, this pass's scan, then what to hold, head and delete.
    try:
        reg = vr.registry_path()
    except DataPathError as exc:
        print(f"version registry address unresolved ({exc}); no families this pass", file=sys.stderr)
        reg = None
    lines = ledger_lines()
    led = memory_plan.index_ledger(lines)
    check = SendCheck(api, bank, key, lines)  # every delete path below asks it first
    mplan = memory_plan.Plan()
    state = vr.State()
    result = doc_scan.ScanResult(complete=False)
    roots = None  # the scope roots, read only when the registry is skipped
    if reg is not None:
        state, result, skipped_why = prepare_memory(root, reg, doc_scan.default_cache_path(), args.dry_run,
                                                    scan_empty=args.detect)
        for err in result.errors:  # every pass: why the scan was incomplete, if it was
            print(f"  scan: {err}", file=sys.stderr)
        if state is None:
            print(f"version registry skipped this pass ({skipped_why}); no deletions and no header changes, "
                  "and only files that cannot belong to a family are sent", file=sys.stderr)
            if not result.roots:
                try:
                    roots = doc_scope._default_roots()
                except (DataPathError, OSError, RuntimeError) as exc:
                    print(f"  scope roots unresolved ({exc}); sending nothing this pass", file=sys.stderr)
                    return 1
            else:
                roots = result.roots
            dropped = [p for p, _ in send if _could_be_member(p, roots)]
            if named is not None:
                refused += [(p, "could belong to a version family and the registry is skipped this pass")
                            for p in dropped]
            send = [(p, s) for p, s in send if p not in set(dropped)]
        else:
            if args.detect:
                state = run_detector(root, reg, state, result, args.dry_run)
            used_today = _family_deletions_today()
            mplan = memory_plan.cap_daily(
                family_plan(state, led, result, root, args.allow_deletes, check), used_today)
    held = mplan.hold | mplan.skip
    fps = mplan.fingerprints
    todo = [p for p, _ in send if p not in held and needs_send(root, p, seen, fps)]
    retry: dict[str, int] = {}
    if named is None:
        # a failed extraction goes again ahead of the queue, so `--limit` never starves it
        retry = resend_failed(api, bank, key, seen, {p for p, _ in send} - held, set(todo))
        todo = list(retry) + todo
    queued = set(todo)  # waiting to be sent, this pass or behind --limit: a pending move's new path
    if named is not None:
        # every existing filter still applies; only ordering and --limit/--sample are bypassed
        for p, _ in send:
            if p in held:
                refused.append((p, mplan.reasons.get(p, "held by the version registry (history or unavailable)")))
            elif p not in todo:
                print(f"  named path unchanged since its last send, not sent again: {p}", file=sys.stderr)
        for e in unmatched:
            print(f"  named path matches no tracked or unignored file, nothing sent: {e}", file=sys.stderr)
        for p, why in refused:
            print(f"  named path refused: {p}: {why}", file=sys.stderr)
    elif args.sample and args.limit and len(todo) > args.limit:
        step = len(todo) / args.limit
        todo = [todo[int(i * step)] for i in range(args.limit)]
    elif args.limit:
        todo = todo[: args.limit]
    print(f"sending {len(todo)} file(s) to bank {bank} in batches of {args.batch}" + (" (dry run)" if args.dry_run else ""),
          file=sys.stderr)
    # A request carries up to `--batch` whole files; a file sent in parts goes one part per
    # request. Its ledger line is written only after its last part was accepted, so a run
    # that fails midway sends the whole file again next time. Requests keep the order of
    # `todo`, so a `wave-last.txt` file never goes ahead of one queued before it. The hash
    # and stat for the ledger are taken BEFORE the text is read: a file edited while the
    # run is under way is then recorded with its old hash and sent again next time, never
    # recorded with the new hash over content the server never received.
    dom = domains_for(root, todo, load_domain_labels(), args.dry_run) if todo else {}
    requests: list[tuple[list[str], list[dict]]] = []
    small: list[str] = []
    meta: dict[str, dict] = {}
    tags: dict[str, list[str]] = {}  # what each file is tagged with, for the dry run's lines
    first: dict[str, int] = {}  # a file in parts: the number of the request carrying its first part
    for p in todo:
        m = {**_ledger_meta(root / p), "ts": document_date(root / p, p)[0].isoformat()}
        items = items_for(root, p, args.max_bytes, mplan.upload.get(p, ""), m["ts"], dom[p])
        tags[p] = items[0]["tags"]
        if len(items) > 1:
            meta[p] = m
            if small:
                requests.append((small, [])); small = []
            first[p] = len(requests) + 1
            requests += [([p] if k == len(items) - 1 else [], [it]) for k, it in enumerate(items)]
            continue
        small.append(p)
        if len(small) == args.batch:
            requests.append((small, [])); small = []
    if small:
        requests.append((small, []))
    ops: dict[str, list[str]] = {}
    for n, (done, items) in enumerate(requests, 1):
        if args.dry_run:
            for p in done:
                print(f"  upload {p}" + (f" in {n - first[p] + 1} parts" if items else "")
                      + (" with version header" if p in mplan.upload else "")
                      + f" tags {' '.join(tags[p])}", file=sys.stderr)
            continue
        if not items:
            for p in done:
                meta[p] = {**_ledger_meta(root / p), "ts": document_date(root / p, p)[0].isoformat()}
            items = [items_for(root, p, args.max_bytes, mplan.upload.get(p, ""), meta[p]["ts"], dom[p])[0]
                     for p in done]
        try:
            resp = _api("POST", f"{api}/v1/default/banks/{bank}/memories", key, {"items": items, "async": True})
        except urllib.error.HTTPError as exc:
            print(f"  batch {n}: HTTP {exc.code} {exc.read()[:300]!r}", file=sys.stderr)
            return 1
        except (urllib.error.URLError, OSError) as exc:
            print(f"  batch {n}: {exc}", file=sys.stderr)
            return 1
        op = resp.get("operation_id")
        path_of = items[0]["document_id"].split("#part-")[0]
        ops.setdefault(path_of, []).append(op)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for p in done:
            rec = {"path": p, "sha256": meta[p]["sha256"], "operation_id": op, "sent": now,
                   "bytes": meta[p]["bytes"], "mtime": meta[p]["mtime"], "ctime_ns": meta[p]["ctime_ns"],
                   "ts": meta[p]["ts"]}
            if len(ops.get(p, [])) > 1:
                rec["operation_ids"] = ops[p]
            if p in retry:
                rec["retry"] = retry[p]
            if p in fps:
                rec["header_fp"] = fps[p]
            print(json.dumps(rec), flush=True)
        print(f"  batch {n}: {len(done) or 0} file(s) completed, {len(items)} document(s), operation {op}",
              file=sys.stderr)
        time.sleep(args.pause)
    _print_memory_plan(mplan, args.allow_deletes, args.dry_run)
    # an entry that matched nothing is most likely a mistyped path: say so in the exit code too
    miss = 2 if unmatched else 0
    rc = 0
    if not (args.dry_run or reg is None or state is None):
        def replan(st: vr.State) -> memory_plan.Plan:
            return memory_plan.cap_daily(family_plan(st, led, result, root, args.allow_deletes, check),
                                         _family_deletions_today())

        rc = finish_deletions(mplan, args.allow_deletes, reg, state.generation, replan, api, bank, key)
    if named is not None:
        return rc or miss  # a named push sends what it names; the mirror step belongs to the full pass
    if rc == 0:
        if reg is None:
            print("mirror: version registry address unresolved; no mirror deletions this pass", file=sys.stderr)
        else:
            # paths another rule decides this pass: the memory plan's, every path of a managed
            # family, or, with the registry skipped, every path that could belong to one
            keep = held | set(mplan.upload) | set(mplan.delete_path.values()) | set(todo)
            keep |= family_paths(state) if state is not None else {p for p in led if _could_be_member(p, roots)}
            already = len(mplan.delete) if args.allow_deletes and not mplan.blocked else 0
            rc = run_mirror(root, led, keep, skip_prefixes(), memory_plan.uploaded_count(led), already,
                            args.dry_run, api, bank, key, args.max_bytes,
                            lambda: memory_plan.index_ledger(ledger_lines()), check, queued)
    return rc or miss


def _key() -> str:
    return Path(_setting("HINDSIGHT_SYNC_KEY_FILE", str(Path.home() / ".config/hindsight/secrets/hindsight-api-key"))
                ).expanduser().read_text(encoding="utf-8").strip()


#: A failure created more than this many seconds before a path's latest send belongs to
#: an earlier version of the file and is not reported.
FAILURE_GRACE_S = 120


def _current_failure(rec: dict | None, op: dict) -> bool:
    """Does this failed operation belong to the path's latest send? A requeue line and
    a deletion line (no send time) have nothing to fail; an operation with no creation
    time is taken as the send's own."""
    if not rec or rec.get("requeue") or "sent" not in rec:
        return False
    if not op.get("created_at"):
        return True
    created = datetime.fromisoformat(op["created_at"])
    return created.timestamp() >= datetime.fromisoformat(rec["sent"]).timestamp() - FAILURE_GRACE_S


def ledger_failed_paths(seen: dict[str, dict], ops: dict[str, dict]) -> dict[str, str]:
    """Ledger paths whose latest send maps to an operation the server reports `failed`,
    read from the ledger's own operation ids (HOS-182). That operation is usually the
    batch the POST returned, a parent with no `document_id`, so the failed-operations
    listing with `exclude_parents` never shows it; `ops` is each id's single-operation read."""
    out: dict[str, str] = {}
    for path, rec in seen.items():
        for op_id in rec.get("operation_ids") or [rec.get("operation_id")]:
            op = ops.get(op_id) if op_id else None
            if op and op.get("status") == "failed" and _current_failure(rec, op):
                out[path] = (op.get("error_message") or "")[:160]
                break
    return out


def failed_paths(api: str, bank: str, key: str, seen: dict[str, dict]) -> dict[str, str]:
    """Ledger paths named by a failed CHILD operation in the server's listing, with the
    error. This sees a document's own failure when the ledger's operation did not fail;
    a failure on the ledger's operation itself is `ledger_failed_paths()`'s."""
    out: dict[str, str] = {}
    offset = 0
    while True:
        page = _api("GET", f"{api}/v1/default/banks/{bank}/operations?status=failed&type=retain"
                           f"&exclude_parents=true&limit=100&offset={offset}", key)
        ops = page.get("operations", [])
        for op in ops:
            path = (op.get("document_id") or "").split("#part-")[0]
            if _current_failure(seen.get(path), op):
                out[path] = (op.get("error_message") or "")[:160]
        offset += len(ops)
        if not ops or offset >= page.get("total", 0):
            return out


#: How many times a full pass sends a file again after the server failed to extract it
#: (HOS-200). The resend count rides on the ledger line as `retry`; a line at the limit
#: whose send failed again is given up and printed every pass until the file changes,
#: so one document that always fails (the 3,600 s wall on a large file) cannot loop.
FAILED_RESEND_LIMIT = 2


def failed_operations(api: str, bank: str, key: str) -> list[dict]:
    """Every operation the server lists as failed, all types, paged to the end. Parents
    are included: MEASURED 2026-10-05 the listing without `exclude_parents` and `type`
    carried 96 operations, the batch parents the ledger records among them, and matched
    against the ledger it named the same 41 files as `status`, in one request."""
    out: list[dict] = []
    while True:
        page = _api("GET", f"{api}/v1/default/banks/{bank}/operations?status=failed&limit=100&offset={len(out)}", key)
        ops = page.get("operations", [])
        out += ops
        if not ops or len(out) >= page.get("total", 0):
            return out


def failed_since_send(seen: dict[str, dict], ops: list[dict]) -> dict[str, str]:
    """Ledger paths whose latest send failed on the server, with the error: the ledger's
    own operation ids (`ledger_failed_paths`) and a failed retain naming the document."""
    out: dict[str, str] = {}
    for op in ops:
        path = (op.get("document_id") or "").split("#part-")[0]
        if path and op.get("operation_type") in ("retain", "batch_retain") and _current_failure(seen.get(path), op):
            out[path] = (op.get("error_message") or "")[:160]
    return {**out, **ledger_failed_paths(seen, {o["id"]: o for o in ops if o.get("id")})}


def resend_failed(api: str, bank: str, key: str, seen: dict[str, dict], sendable: set[str],
                  todo: set[str]) -> dict[str, int]:
    """The files to send again because their extraction failed, each with its resend
    number, in path order. A file already going (its bytes changed) and one this pass
    would not send are left to the plan; one at `FAILED_RESEND_LIMIT` is given up. An
    unreadable listing resends nothing this pass and says so."""
    try:
        failed = failed_since_send(seen, failed_operations(api, bank, key))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"failed extractions unreadable ({type(exc).__name__}: {exc}); none sent again this pass", file=sys.stderr)
        return {}
    out: dict[str, int] = {}
    for path, err in sorted(failed.items()):
        n = int(seen[path].get("retry") or 0)
        if path in todo or path not in sendable:
            continue
        if n >= FAILED_RESEND_LIMIT:
            print(f"  failed extraction given up after {n} resend(s): {path}: {err}", file=sys.stderr)
            continue
        out[path] = n + 1
        print(f"  send again after a failed extraction: {path} (resend {n + 1} of {FAILED_RESEND_LIMIT}): {err}",
              file=sys.stderr)
    return out


def cmd_status(args) -> int:
    api, bank, key = _conn()
    seen = last_sent()
    ids = {op for r in seen.values() for op in (r.get("operation_ids") or [r.get("operation_id")]) if op}
    ops: dict[str, dict] = {}
    counts: dict[str, int] = {}
    for op in sorted(ids):
        try:
            ops[op] = _api("GET", f"{api}/v1/default/banks/{bank}/operations/{op}", key)
            st = ops[op].get("status", "unknown")
        except urllib.error.HTTPError as exc:
            st = f"http-{exc.code}"
        counts[st] = counts.get(st, 0) + 1
    print(f"{len(seen)} file(s) in the ledger, {len(ids)} operation(s): "
          + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())), file=sys.stderr)
    failed = {**failed_paths(api, bank, key, seen), **ledger_failed_paths(seen, ops)}
    for path, err in sorted(failed.items()):
        print(f"  failed on the server: {path}: {err}", file=sys.stderr)
        if args.requeue_failed:
            # a ledger line, appended by the caller like push's, that makes the next push send it again
            print(json.dumps({"path": path, "requeue": True, "reason": err,
                              "sent": datetime.now(timezone.utc).isoformat(timespec="seconds")}), flush=True)
    return 0


# ============================================================
# Cleanup: documents of the bank with no source file (HOS-183)
# ============================================================

#: An id that starts `<word>:` is not a repository path: `conversation:` (a session),
#: `gitlog:` (git history), `survey-baseline:` (the coding-agent plugin's survey marker)
#: and any other such kind. None is ever planned.
NOT_A_PATH = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*:")
#: A tag of that same `<word>:` shape marks a document another tool wrote (the plugin's
#: `source:`, `harness:`, `knowledge:`, `survey-state:`, `gitlog-head:`); this script's own
#: are `area:` and `domain:` (HOS-230) on files and `session:` on the turns the removed
#: `turns` command sent. The plugin's uploads have plain ids, and
#: its initiative markers carry `relatedPageId`, so a rule on ids alone would plan them.
OUR_TAG_PREFIXES = ("area:", "domain:", "session:")
CLEANUP_DEFAULT_LIMIT = 50
LIST_PAGE = 1000
PLAN_KIND = "hindsight-sync cleanup plan"


# ============================================================
# Mirror: a file that left the repository leaves the bank (HOS-188)
# ============================================================


def mirror_reason(path: str, files: set[str], root: Path, skip: tuple[str, ...],
                  prefix: str | None = None) -> str | None:
    """Why a path the ledger shows as sent no longer belongs in the bank, or None.

    Under a `wave-skip.txt` prefix or inside a `_work/` folder, tracked or not; a
    machine-written memory record under `prefix` (HOS-200), its file left on disk; or no
    longer git-tracked AND not on disk. A file on disk that git does not track is left
    alone, as `cleanup` leaves it. The archive and type rules of `excluded_why()` are not
    asked here: a document they would refuse today may have been sent under an earlier
    rule, and taking it out is a decision nobody has made."""
    if NOT_A_PATH.match(path):
        return None
    if in_work_dir(path):
        return WORK_REASON
    under = next((p for p in skip if path.startswith(p)), None)
    if under:
        return f"under the wave-skip.txt prefix {under}"
    if machine_written(root, path, prefix):
        return MACHINE_REASON
    if path in files:
        return None
    here = (root / path).resolve()
    if here.is_relative_to(root.resolve()) and here.exists():
        return None
    return LEFT_REASON


LEFT_REASON = "the file is no longer in the data repository"
MOVE_REASON = "moved: the same content is in the bank under a path still in the repository"
TRIVIAL_REASON = "its content is empty or whitespace only, which proves no move"


def family_paths(state: vr.State) -> set[str]:
    """Every path, and its `-extract.md` twin, of a member of a family the memory plan
    manages (confirmed, with an actual version): its own rules decide when those go."""
    out: set[str] = set()
    for fam in state.families.values():
        if not fam.confirmed or fam.actual is None:
            continue
        for m in list(fam.members.values()) + list(fam.proposed.values()):
            out.add(m.path)
            twin = doc_scope.twin_of(Path(), m.path)
            if twin is not None:
                out.add(PurePosixPath(twin).as_posix())
    return out


def stale_shape_ids(path: str, entry: dict, root: Path, max_bytes: int) -> list[str]:
    """Ids of `path` the ledger shows sent and not deleted that belong to an earlier SHAPE
    of the file (HOS-193): the whole document once the file goes in parts, or `#part-NN`
    ids the file no longer has. An id is stale only when neither the last upload nor the
    file as it would be split now carries it, so a ledger line that misrecords the parts
    can never take a live document out."""
    if entry["upload"] is None:
        return []
    current = set(memory_plan._ids_of_upload(path, entry["upload"]))
    live = [i for i in entry["ids"] if i not in entry["deleted"] and i not in current]
    if not live:
        return []
    try:
        n = len(split_text(sendable_text(root / path), max_bytes))
    except OSError as exc:  # unreadable now: prove nothing, delete nothing
        print(f"  mirror: {path} unreadable ({exc}); its earlier shape is left", file=sys.stderr)
        return []
    now = {path} if n == 1 else {f"{path}#part-{i:02d}" for i in range(1, n + 1)}
    return [i for i in live if i not in now]


SHAPE_REASON = "an earlier shape of a file still in the repository (whole beside parts, or a part it no longer has)"


def mirror_candidates(led: dict[str, dict], files: set[str], root: Path, skip: tuple[str, ...],
                      keep: set[str], max_bytes: int = DEFAULT_MAX_BYTES,
                      prefix: str | None = None) -> list[tuple[str, str, str]]:
    """`(id, ledger path, reason)` for every id the ledger shows this script sent and no
    `deleted` line has removed, whose path `mirror_reason()` names, or which
    `stale_shape_ids()` names for a file that stays, in path order. Ids come from the
    ledger alone, so a document this script never sent is never chosen. `keep` holds the
    paths another rule decides this pass, and the paths this pass uploads."""
    out: list[tuple[str, str, str]] = []
    for path in sorted(led):
        entry = led[path]
        if path in keep or entry["upload"] is None:
            continue
        why = mirror_reason(path, files, root, skip, prefix)
        if why is not None:
            out += [(i, path, why) for i in entry["ids"] if i not in entry["deleted"]]
        elif not NOT_A_PATH.match(path):
            out += [(i, path, SHAPE_REASON) for i in stale_shape_ids(path, entry, root, max_bytes)]
    return out


def move_proof(new: str, sha: str, entry: dict, files: set[str], root: Path, skip: tuple[str, ...],
               prefix: str | None, check: SendCheck) -> str | None:
    """Why `new` does NOT prove that content `sha` is in the bank, or None when it does: it
    belongs in the repository (`mirror_reason()` names nothing), its last send carried `sha`
    and none of that send's ids was deleted since, its bytes on disk still hash to `sha` and
    are not trivial (`trivial_content`), and every operation of that send, children
    included, is `completed` (`SendCheck.why_unproven`: a send in flight, an unreadable
    status or a FAILED extraction proves nothing, failed being settled but not recallable)."""
    up = entry["upload"]
    if up is None or up.get("sha256") != sha:
        return "its last send carried other content"
    if mirror_reason(new, files, root, skip, prefix) is not None:
        return "it is not in the repository"
    ids = memory_plan._ids_of_upload(new, up)
    if any(i in entry["deleted"] for i in ids):
        return "its last send was deleted since"
    try:
        if sha256(root / new) != sha:
            return "its content changed since its last send"
        if trivial_content(root / new):
            return TRIVIAL_REASON
    except OSError as exc:
        return f"it is unreadable ({type(exc).__name__})"
    for i in ids:
        wait = check.why_unproven(i)
        if wait:
            return wait
    return None


def move_targets(due: list[tuple[str, str, str]], led: dict[str, dict], files: set[str], root: Path,
                 skip: tuple[str, ...], keep: set[str], prefix: str | None, check: SendCheck) -> dict[str, str]:
    """`{old path: new path}` for every path of `due` that left the repository (`LEFT_REASON`)
    and was MOVED: its last sent sha256 is the last sent sha256 of a path in the repository
    now that `move_proof()` accepts. A path `keep` names (uploaded this pass, or decided by
    the version registry) proves nothing. Several candidates: the first that proves, in path
    order. A path left for any other reason (wave-skip, `_work/`, machine-written, an earlier
    shape) is never a move."""
    left = sorted({path for _, path, why in due if why == LEFT_REASON})
    if not left:
        return {}
    by_sha: dict[str, list[str]] = {}
    for path, entry in led.items():
        up = entry["upload"]
        if up is None or not up.get("sha256") or path in keep or NOT_A_PATH.match(path):
            continue
        by_sha.setdefault(up["sha256"], []).append(path)
    out: dict[str, str] = {}
    for old in left:
        sha = led[old]["upload"].get("sha256")
        for new in sorted(by_sha.get(sha) or []):
            if new != old and move_proof(new, sha, led[new], files, root, skip, prefix, check) is None:
                out[old] = new
                break
    return out


def pending_moves(left: list[str], led: dict[str, dict], files: set[str], root: Path, skip: tuple[str, ...],
                  prefix: str | None, queued: set[str], check: SendCheck) -> dict[str, tuple[str, str]]:
    """`{old path: (new path, why)}` for every path of `left` (paths that left the repository
    and are not a proven move) whose content is still in the repository, on its way into the
    bank under a new path: a path in the repository whose bytes on disk hash to the old
    path's last sent sha256 and which `queued` names (sent this pass, or waiting behind
    `--limit`), or whose last send carried that sha256 and has not completed: in flight,
    unreadable, or failed until a resend (HOS-200) completes. Deleting the old
    ids now would leave the content recallable nowhere until that send settles; deferred, the
    old path becomes a proven move on a later pass. Trivial content makes no move pending.
    The first new path in path order is named."""
    want: dict[str, list[str]] = {}
    sizes: set[int | None] = set()
    for old in left:
        up = led[old]["upload"]
        if up and up.get("sha256"):
            want.setdefault(up["sha256"], []).append(old)
            sizes.add(up.get("bytes"))
    if not want:
        return {}
    found: dict[str, tuple[str, str]] = {}

    def offer(new: str, sha: str, why: str) -> None:
        for old in want.get(sha, ()):
            if old != new and (old not in found or new < found[old][0]):
                found[old] = (new, why)

    def here(new: str, sha: str | None = None) -> str | None:
        """The sha256 of `new` on disk when it belongs in the repository and is not trivial."""
        if NOT_A_PATH.match(new) or mirror_reason(new, files, root, skip, prefix) is not None:
            return None
        f = root / new
        try:
            if None not in sizes and f.stat().st_size not in sizes:
                return None
            got = sha256(f)
            if got not in want or (sha is not None and got != sha) or trivial_content(f):
                return None
        except OSError:
            return None
        return got

    for new in sorted(queued):
        got = here(new)
        if got:
            offer(new, got, "the new path is queued for sending")
    for new, entry in sorted(led.items()):
        up = entry["upload"]
        sha = up.get("sha256") if up else None
        if sha not in want or new in queued:
            continue
        ids = memory_plan._ids_of_upload(new, up)
        if any(i in entry["deleted"] for i in ids) or not here(new, sha):
            continue
        wait = next((w for w in map(check.why_unproven, ids) if w), None)
        if wait:
            offer(new, sha, f"the new path's send has not completed: {wait}")
    return found


def mirror_cap(uploaded: int, already: int = 0) -> int:
    """How many ids the mirror step may delete this pass: the guard of `memory_plan`
    (`MAX_DELETES`, and `MAX_FRACTION` of the uploaded paths, at least one), less the
    family deletions this same pass already plans. Operator decision, 2026-10-03, on more than that:
    "Delete in portions." (translated) The rest waits for the next passes; nothing waits for a word."""
    cap = max(1, min(memory_plan.MAX_DELETES, int(memory_plan.MAX_FRACTION * uploaded)))
    return max(0, cap - already)


def run_mirror(root: Path, led: dict[str, dict], keep: set[str], skip: tuple[str, ...], uploaded: int,
               already: int, dry_run: bool, api: str, bank: str, key: str,
               max_bytes: int = DEFAULT_MAX_BYTES, reread=None, check: SendCheck | None = None,
               queued: set[str] | None = None) -> int:
    """Delete up to `mirror_cap()` ids whose file left the repository under `root`, `skip`
    being its `wave-skip.txt` prefixes. Per id, git is asked again and an id whose file is
    back stops the step; an id whose last send `check` says is still in flight is deferred
    to a later pass and takes no place in the portion; then a `delete_intent` line, the
    DELETE, and `deleted` (a 404 counts) or `delete_failed`, which stops the rest.
    `reread` returns the ledger index as it is now (the caller's, so this function
    resolves no data root of its own); without it an earlier-shape id is not deleted.
    Without `check` nothing is deleted: a missing check is never read as a settled send.
    A proven move goes outside the portion (`run_moves`); a pending one (`pending_moves`,
    `queued` being the paths waiting to be sent, this pass or later) is printed and left."""
    try:
        files = set(tracked(root))
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"mirror: tree unreadable ({type(exc).__name__}: {exc}); no mirror deletions this pass",
              file=sys.stderr)
        return 0
    if not files:
        print("mirror: git lists no tracked file in the data repository; no mirror deletions this pass",
              file=sys.stderr)
        return 0
    prefix = _auto_memory_prefix(root)
    due = due_all = mirror_candidates(led, files, root, skip, keep, max_bytes, prefix)
    if not due:
        return 0
    if check is None:
        print("mirror: no send-status check given; no mirror deletions this pass", file=sys.stderr)
        return 0
    cap = mirror_cap(uploaded, already)
    moved = move_targets(due, led, files, root, skip, keep, prefix, check)
    moves = [(i, p, moved[p]) for i, p, w in due if w == LEFT_REASON and p in moved]
    rest = sorted({p for _, p, w in due if w == LEFT_REASON and p not in moved})
    waiting = pending_moves(rest, led, files, root, skip, prefix, queued or set(), check) if rest else {}
    due = [d for d in due if not (d[2] == LEFT_REASON and (d[1] in moved or d[1] in waiting))]
    pend = [(i, p) for i, p, w in due_all if w == LEFT_REASON and p in waiting]
    if pend:
        print(f"mirror: {len(pend)} document(s) of {len(waiting)} moved file(s) whose new path is not yet "
              "in the bank: pending, deferred to a later pass, outside the portion", file=sys.stderr)
        for doc_id, path in pend:
            new, why = waiting[path]
            print(f"  mirror pending-move {doc_id} -> {new}: {why}", file=sys.stderr)
    if moves:
        rc = run_moves(moves, root, led, skip, prefix, dry_run, api, bank, key, check)
        if rc is not None:
            return rc
        if not due:
            return 0
    why: dict[str, int] = {}
    for _, _, w in due:
        why[w] = why.get(w, 0) + 1
    for w, n in sorted(why.items()):
        print(f"mirror: {n} document(s): {w}", file=sys.stderr)
    print(f"mirror: {len(due)} document(s) whose file left the data repository; at most {cap} deleted this pass"
          + (" (dry run)" if dry_run else ""), file=sys.stderr)
    done = deferred = 0
    for doc_id, path, why in due:
        if done >= cap:
            break
        wait = check.why_wait(doc_id)
        if wait:
            deferred += 1
            print(f"  mirror defer {doc_id}: {wait}; asked again next pass", file=sys.stderr)
            continue
        if dry_run:
            print(f"  mirror delete {doc_id} [{why}] (dry run)", file=sys.stderr)
            done += 1
            continue
        try:
            still = mirror_reason(path, set(tracked(root)), root, skip, prefix)
            if still is None and why == SHAPE_REASON:
                # asked again of the ledger as it is now: a pass running beside this one
                # may have sent the file in this id's shape since
                fresh = reread().get(path) if reread else None
                if fresh and doc_id in stale_shape_ids(path, fresh, root, max_bytes):
                    still = SHAPE_REASON
        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"  mirror: tree unreadable before {doc_id} ({exc}); stopping mirror deletions", file=sys.stderr)
            return 0
        if still is None:
            print(f"  mirror: {doc_id}: " + ("no longer an earlier shape" if why == SHAPE_REASON else "its file is back")
                  + "; stopping mirror deletions this pass", file=sys.stderr)
            return 0
        print(json.dumps({"path": path, "delete_intent": doc_id, "mirror": still,
                          "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}), flush=True)
        if not delete_recorded(api, bank, key, doc_id, {"path": path, "mirror": still}):
            print("  stopping mirror deletions, asked again next pass", file=sys.stderr)
            return 1
        done += 1
    rest = len(due) - done - deferred
    print(f"mirror: {done} deleted" + (" (dry run)" if dry_run else "")
          + (f", {deferred} deferred until their send settles" if deferred else "")
          + (f", {rest} left for the next passes" if rest else ""), file=sys.stderr)
    return 0


def run_moves(moves: list[tuple[str, str, str]], root: Path, led: dict[str, dict], skip: tuple[str, ...],
              prefix: str | None, dry_run: bool, api: str, bank: str, key: str, check: SendCheck) -> int | None:
    """Delete the ids `(id, old path, new path)` of files that MOVED, all of them this pass and
    outside the mirror's portion: the portion guards against losing memory by mass deletion,
    and `move_proof()` has shown the content is in the bank under the new path. Like the
    `--allow-deletes`-free mirror it belongs to, it needs no flag. Per id: the old id's own
    send must have settled (else deferred); git is asked again and an old file that is back
    stops every mirror deletion this pass; the new path is proved again, and an id whose proof
    fell away is left for the ordinary rules of the next pass. The ledger lines are the
    mirror's, with `mirror` set to `MOVE_REASON` and `moved_to` naming the new path. Returns
    an exit code when the mirror must stop here, None to go on with the portion."""
    print(f"mirror: {len(moves)} document(s) of {len({p for _, p, _ in moves})} moved file(s): the same content "
          "is in the bank under the new path; all deleted this pass, outside the portion"
          + (" (dry run)" if dry_run else ""), file=sys.stderr)
    done = deferred = kept = 0
    for doc_id, path, new in moves:
        wait = check.why_wait(doc_id)
        if wait:
            deferred += 1
            print(f"  mirror defer {doc_id}: {wait}; asked again next pass", file=sys.stderr)
            continue
        if dry_run:
            print(f"  mirror move {doc_id} -> {new} (dry run)", file=sys.stderr)
            done += 1
            continue
        try:
            files = set(tracked(root))
        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"  mirror: tree unreadable before {doc_id} ({exc}); stopping mirror deletions", file=sys.stderr)
            return 0
        if mirror_reason(path, files, root, skip, prefix) != LEFT_REASON:
            print(f"  mirror: {doc_id}: its file is back; stopping mirror deletions this pass", file=sys.stderr)
            return 0
        lost = move_proof(new, led[path]["upload"].get("sha256"), led[new], files, root, skip, prefix, check)
        if lost:
            kept += 1
            print(f"  mirror: {doc_id}: {new} no longer proves the move ({lost}); left for the next pass",
                  file=sys.stderr)
            continue
        base = {"path": path, "mirror": MOVE_REASON, "moved_to": new}
        print(json.dumps({**base, "delete_intent": doc_id,
                          "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}), flush=True)
        if not delete_recorded(api, bank, key, doc_id, base):
            print("  stopping mirror deletions, asked again next pass", file=sys.stderr)
            return 1
        done += 1
    print(f"mirror: {done} moved deleted" + (" (dry run)" if dry_run else "")
          + (f", {deferred} deferred until their send settles" if deferred else "")
          + (f", {kept} left for the next pass" if kept else ""), file=sys.stderr)
    return None


def _conn() -> tuple[str, str, str]:
    """The server, the bank and the key, from the operator's settings."""
    return (_setting("HINDSIGHT_SYNC_API_URL", "http://127.0.0.1:8888").rstrip("/"),
            _setting("HINDSIGHT_SYNC_BANK", "main"), _key())


def list_documents(api: str, bank: str, key: str, attempts: int = 3) -> tuple[dict[str, dict] | None, str]:
    """Every document of the bank by id, paged to the end, or `(None, why)`.

    The listing counts as complete only when every page carried the same `total`
    and the distinct ids add up to exactly that total; a bank that changed while
    paging is listed again, up to `attempts` times. A transport error is raised to
    the caller, which plans and deletes nothing."""
    why = "no attempt made"
    for _ in range(attempts):
        docs: list[dict] = []
        first_total = None
        offset = 0
        moved = False
        while True:
            page = _api("GET", f"{api}/v1/default/banks/{bank}/documents?limit={LIST_PAGE}&offset={offset}", key)
            items, total = page.get("items"), page.get("total")
            if not isinstance(items, list) or not isinstance(total, int):
                return None, "the listing carried no items or no total"
            if first_total is None:
                first_total = total
            elif total != first_total:
                why, moved = f"the bank changed while paging (total {first_total} -> {total})", True
                break
            docs += [it if isinstance(it, dict) else {} for it in items]
            offset += len(items)
            if not items or offset >= total:
                break
        if moved:
            continue
        by_id = {str(d.get("id") or ""): d for d in docs}
        if "" in by_id or len(by_id) != len(docs) or len(docs) != first_total:
            why = f"{len(by_id)} distinct ids against a reported total of {first_total}"
            continue
        return by_id, ""
    return None, why


def owner_mark(doc: dict) -> str | None:
    """Why a listed document belongs to something other than a repository file, or None."""
    if NOT_A_PATH.match(str(doc.get("id") or "")):
        return "not a repository path (session, git history, plugin)"
    meta = doc.get("document_metadata")
    if isinstance(meta, dict) and meta.get("relatedPageId"):
        return "tied to a knowledge page"
    if any(NOT_A_PATH.match(str(t)) and not str(t).startswith(OUR_TAG_PREFIXES) for t in doc.get("tags") or []):
        return "written by another tool (a `<word>:` tag not this script's)"
    return None


def no_source_reason(doc: dict, led: dict[str, dict], files: set[str], root: Path,
                     skip: tuple[str, ...]) -> str | None:
    """Why a listed document has no source file, or None when it must never be planned:
    one `owner_mark()` names, or one whose file is git-tracked or on disk."""
    if owner_mark(doc) is not None:
        return None
    doc_id = str(doc["id"])
    base = doc_id.split("#part-")[0]
    if base in files:
        return None
    here = (root / base).resolve()
    if here.is_relative_to(root.resolve()) and here.exists():
        return None
    if (led.get(base) or {}).get("upload") is None:
        return "never sent by hindsight-sync (not in its ledger), and no file at that path"
    under = next((p for p in skip if base.startswith(p)), None)
    if under:
        return f"sent by hindsight-sync; the file is gone, and its path is under the wave-skip.txt prefix {under}"
    return "sent by hindsight-sync; the file is no longer in the data repository"


def cleanup_candidates(docs: dict[str, dict], led: dict[str, dict], files: set[str], root: Path,
                       skip: tuple[str, ...], prefix: str = "") -> tuple[list[tuple[str, str]], dict[str, int]]:
    """The planned `(id, reason)` pairs in id order, and what was left out and why."""
    out: list[tuple[str, str]] = []
    left: dict[str, int] = {}
    for doc_id in sorted(docs):
        if prefix and not doc_id.startswith(prefix):
            continue
        why = no_source_reason(docs[doc_id], led, files, root, skip)
        if why is not None:
            out.append((doc_id, why))
        else:
            mark = owner_mark(docs[doc_id]) or "its file exists"
            left[mark] = left.get(mark, 0) + 1
    return out, left


def _cleanup_inputs(root: Path) -> tuple[set[str], dict[str, dict], tuple[str, ...]]:
    """The tracked files, the indexed ledger and the skip prefixes, read fresh.
    An empty tracked list is refused: it would make every document look sourceless."""
    files = set(tracked(root))
    if not files:
        raise RuntimeError("git lists no tracked file in the data repository")
    return files, memory_plan.index_ledger(ledger_lines()), skip_prefixes()


def cmd_cleanup_plan(args) -> int:
    root = get_data_root()
    api, bank, key = _conn()
    try:
        files, led, skip = _cleanup_inputs(root)
        docs, why = list_documents(api, bank, key)
    except (urllib.error.URLError, OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"cleanup plan: {type(exc).__name__}: {exc}; planning nothing", file=sys.stderr)
        return 1
    if docs is None:
        print(f"cleanup plan: the document listing is incomplete ({why}); planning nothing", file=sys.stderr)
        return 1
    found, left = cleanup_candidates(docs, led, files, root, skip, args.prefix or "")
    scope = f" under {args.prefix}" if args.prefix else ""
    print(f"bank {bank}: {len(docs)} documents listed; {len(found)} with no source file{scope}. Nothing deleted.")
    for what, n in left.items():
        print(f"  left out, {what}: {n}")
    counts: dict[str, int] = {}
    for _, why in found:
        counts[why] = counts.get(why, 0) + 1
    for why, n in sorted(counts.items()):
        print(f"  by reason, {why}: {n}")
    for n, (doc_id, why) in enumerate(found, 1):
        print(f"{n:6d}  {doc_id}  [{why}]")
    if args.save:
        out = Path(args.save).expanduser()
        body = {"kind": PLAN_KIND, "bank": bank, "made": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "prefix": args.prefix or "", "listed": len(docs), "complete": True,
                "items": [{"n": n, "id": d, "reason": w} for n, (d, w) in enumerate(found, 1)]}
        tmp = out.with_name(out.name + ".tmp")
        tmp.write_text(json.dumps(body, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        tmp.replace(out)
        print(f"plan saved: {out.resolve()}", file=sys.stderr)
    return 0


def parse_numbers(spec: str) -> list[int]:
    """`1-5,8` as [1, 2, 3, 4, 5, 8]; anything else raises ValueError."""
    out: list[int] = []
    for part in spec.split(","):
        lo, _, hi = part.strip().partition("-")
        a, b = int(lo), int(hi or lo)
        if a < 1 or b < a:
            raise ValueError(f"not a number range: {part!r}")
        out += [n for n in range(a, b + 1) if n not in out]
    return out


def _named_targets(args, bank: str) -> tuple[list[str], str]:
    """The ids the operator named, from a saved plan by number or by an explicit prefix, and
    the tag their ledger lines carry. Raises ValueError for anything not named exactly."""
    if bool(args.plan) == bool(args.prefix):
        raise ValueError("name the documents either by --plan FILE --numbers N or by --prefix P, not both")
    if args.prefix:
        if not args.prefix.strip() or NOT_A_PATH.match(args.prefix):
            raise ValueError(f"not a repository path prefix: {args.prefix!r}")
        return [], f"prefix {args.prefix}"
    if not args.numbers:
        raise ValueError("--plan needs --numbers")
    plan = json.loads(Path(args.plan).expanduser().read_text(encoding="utf-8"))
    if plan.get("kind") != PLAN_KIND or plan.get("complete") is not True:
        raise ValueError("the plan file is not a complete cleanup plan")
    if plan.get("bank") != bank:
        raise ValueError(f"the plan is for bank {plan.get('bank')!r}, not {bank!r}")
    by_n = {it["n"]: it["id"] for it in plan.get("items", [])}
    want = parse_numbers(args.numbers)
    missing = [n for n in want if n not in by_n]
    if missing:
        raise ValueError(f"numbers not in the plan: {missing[:10]}")
    return [by_n[n] for n in want], f"plan {Path(args.plan).name}"


def cmd_cleanup_apply(args) -> int:
    """Delete only what the operator named, each id re-checked against a fresh listing, the
    ledger and the tree; at most `--limit` per run; the first failure stops the rest."""
    root = get_data_root()
    api, bank, key = _conn()
    if args.limit < 1:
        print("cleanup apply: --limit must be at least 1; deleting nothing", file=sys.stderr)
        return 2
    try:
        named, tag = _named_targets(args, bank)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(f"cleanup apply: {exc}; deleting nothing", file=sys.stderr)
        return 2
    try:
        files, led, skip = _cleanup_inputs(root)
        docs, why = list_documents(api, bank, key)
    except (urllib.error.URLError, OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"cleanup apply: {type(exc).__name__}: {exc}; deleting nothing", file=sys.stderr)
        return 1
    if docs is None:
        print(f"cleanup apply: the document listing is incomplete ({why}); deleting nothing", file=sys.stderr)
        return 1
    if args.prefix:
        named = [d for d, _ in cleanup_candidates(docs, led, files, root, skip, args.prefix)[0]]
    todo: list[tuple[str, str]] = []
    for doc_id in named:
        if doc_id not in docs:
            print(f"  {doc_id}: not in the bank now, nothing to delete", file=sys.stderr)
            continue
        why = no_source_reason(docs[doc_id], led, files, root, skip)
        if why is None:
            print(f"  {doc_id}: refused, it has a source file now or is not a repository path", file=sys.stderr)
            continue
        todo.append((doc_id, why))
    rest = max(0, len(todo) - args.limit)
    print(f"cleanup apply ({tag}): bank {bank}, {min(len(todo), args.limit)} document(s) named for deletion"
          + (f", {rest} more left for later runs (--limit {args.limit})" if rest else "")
          + (" (dry run)" if args.dry_run else ""), file=sys.stderr)
    # a deferred id takes no place under --limit: the next named id is taken in its place
    check = SendCheck(api, bank, key, ledger_lines())
    done = deferred = 0
    for doc_id, why in todo:
        if done >= args.limit:
            break
        wait = check.why_wait(doc_id)
        if wait:
            deferred += 1
            print(f"  defer {doc_id}: {wait}; run the cleanup again once it settles", file=sys.stderr)
            continue
        done += 1
        if args.dry_run:
            print(f"  delete {doc_id} [{why}] (dry run)", file=sys.stderr)
            continue
        base = doc_id.split("#part-")[0]
        if base in tracked(root) or no_source_reason(docs[doc_id], led, set(), root, skip) is None:
            print(f"  {doc_id}: its file appeared during the run; stopping", file=sys.stderr)
            return 1
        print(json.dumps({"path": base, "delete_intent": doc_id, "cleanup": tag, "reason": why,
                          "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}), flush=True)
        if not delete_recorded(api, bank, key, doc_id, {"path": base, "cleanup": tag}):
            print("  stopping the cleanup; the rest is not deleted", file=sys.stderr)
            return 1
    if deferred:
        print(f"cleanup apply: {deferred} document(s) deferred until their send settles; nothing recorded for them",
              file=sys.stderr)
    return 0


# ============================================================
# Blocked: family deletions the mass-delete guard held back (HOS-264)
# ============================================================

def _uncapped_plan(st: vr.State, led: dict[str, dict], result, root: Path) -> memory_plan.Plan:
    """The memory plan with the per-pass guard lifted, nothing else changed."""
    return memory_plan.plan(st, led, result, memory_plan.uploaded_count(led), root=root,
                            max_deletes=10**9, max_fraction=float("inf"))


def _blocked_token(ids: list[str]) -> str:
    """`N:H`, the count and a short hash of exactly the list the operator saw."""
    digest = hashlib.sha256("\n".join(sorted(ids)).encode("utf-8")).hexdigest()[:12]
    return f"{len(ids)}:{digest}"


def _blocked_inputs(dry_run: bool, out):
    """The planning inputs, the uncapped plan, and None; or an exit code when the
    registry cannot be planned from or the guard blocked nothing."""
    root = get_data_root()
    try:
        reg = vr.registry_path()
    except DataPathError as exc:
        print(f"blocked: version registry address unresolved ({exc})", file=sys.stderr)
        return 1
    state, result, why = prepare_memory(root, reg, doc_scan.default_cache_path(), dry_run, scan_empty=False)
    if state is None:
        print(f"blocked: version registry skipped ({why})", file=sys.stderr)
        return 1
    lines = ledger_lines()
    led = memory_plan.index_ledger(lines)
    normal = memory_plan.plan(state, led, result, memory_plan.uploaded_count(led), root=root)
    if normal.blocked is None:
        print(f"nothing blocked by the guard ({len(normal.delete)} deletion(s) planned for the normal pass)", file=out)
        return 0
    return root, reg, state, result, lines, led, _uncapped_plan(state, led, result, root)


def cmd_blocked_list(args) -> int:
    got = _blocked_inputs(True, sys.stdout)
    if isinstance(got, int):
        return got
    _, _, state, _, _, _, full = got
    fam_of: dict[str, tuple[str, str]] = {}  # member path -> (family id, actual path)
    for fam in state.families.values():
        if not fam.confirmed or fam.actual is None:
            continue
        for m in fam.members.values():
            fam_of[m.path] = (fam.family_id, fam.members[fam.actual].path)
    twin_of = {p.rsplit(".", 1)[0] + "-extract.md": v for p, v in fam_of.items()}
    for doc_id in sorted(full.delete):
        path = full.delete_path[doc_id]
        fid, actual = fam_of.get(path) or twin_of.get(path) or ("?", "?")
        print(f"{doc_id}\t{path}\t{fid}\t{actual}")
    print(f"confirm: {_blocked_token(full.delete)}")
    return 0


def cmd_blocked_delete(args) -> int:
    if not args.dry_run:
        # before the live scan too: a live pass appends registry events, and that is HELM's
        from scripts.utils.clone_guard import require_main_clone
        from scripts.utils.crm_autolog import data_is_readonly
        require_main_clone(__file__)
        if data_is_readonly():
            print("REFUSED: this host's data clone is read-only", file=sys.stderr)
            return 2
    got = _blocked_inputs(args.dry_run, sys.stderr)
    if isinstance(got, int):
        return got
    root, reg, state, result, lines, led, full = got
    token = _blocked_token(full.delete)
    if args.confirm != token:
        print(f"refused: confirmation {args.confirm} does not match the current list {token}; "
              "run blocked list again", file=sys.stderr)
        return 3
    confirmed = set(full.delete)  # taken now: the deferral and the cap below cut `full` in place
    api, bank, key = _conn()
    check = SendCheck(api, bank, key, lines)
    deferred = len(full.delete)
    p = defer_unsettled(full, check)
    deferred -= len(p.delete)
    before = len(p.delete)
    p = memory_plan.cap_daily(p, _family_deletions_today())
    waiting = before - len(p.delete)
    if args.dry_run:
        for doc_id in p.delete:
            print(f"would delete {doc_id} ({p.delete_path[doc_id]})", file=sys.stderr)
        print(f"deferred, send in flight: {deferred}", file=sys.stderr)
        print(f"waiting for a later day: {waiting}", file=sys.stderr)
        return 0
    changed = [False]

    def replan(st: vr.State) -> memory_plan.Plan:
        q = _uncapped_plan(st, led, result, root)
        if not set(q.delete) <= confirmed:
            changed[0] = True
            return memory_plan.Plan()
        return memory_plan.cap_daily(defer_unsettled(q, check), _family_deletions_today())

    rc = deletion_phase(reg, state.generation, p, replan, api, bank, key)
    if changed[0]:
        print("refused: the list changed while deleting; run blocked list again", file=sys.stderr)
        return 3
    print(f"waiting for a later day: {waiting}", file=sys.stderr)
    return rc


# ============================================================
# Capture check: every interactive session reached the bank (HOS-200)
# ============================================================

#: The Coding Agents plugin's own settings; only `optInOnly`, `optInPaths` and
#: `mapPathToBank` are read, and nothing from the file is ever printed.
CAPTURE_CONFIG = "~/.hindsight/coding-agent.json"
CAPTURE_PROJECTS = "~/.claude/projects"
#: A transcript names its working directory on its first user or assistant record; a
#: file with none in this many lines holds no conversation.
CAPTURE_HEAD_LINES = 500


def transcript_head(f: Path) -> dict | None:
    """The working directory and entrypoint of a session transcript, from its first user
    or assistant record, or None when it holds no conversation."""
    with f.open(encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            if i >= CAPTURE_HEAD_LINES:
                return None
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and rec.get("type") in ("user", "assistant") and rec.get("cwd"):
                return {"cwd": str(rec["cwd"]), "entrypoint": str(rec.get("entrypoint") or "")}
    return None


def opted_root(cwd: str, roots: list[str]) -> str | None:
    """The opted-in root `cwd` sits in (the longest, when they nest), or None."""
    for r in sorted(roots, key=len, reverse=True):
        base = r.rstrip("/") or "/"
        if cwd == base or cwd.startswith(base.rstrip("/") + "/"):
            return r
    return None


def cmd_capture_check(args) -> int:
    """Every interactive session transcript under the plugin's opted-in roots, modified in
    the window and settled, must have `conversation:<session-id>` in its bank. A headless
    run (entrypoint `sdk-cli`: the plugin's survey agents and probes, started with
    HINDSIGHT_DISABLE_HOOKS=1) is absent by design and is never a miss; the transcript does
    not record that variable, so every headless run is classified that way. Exit 1 when an
    interactive session is missing, 2 when the check could not be made."""
    try:
        cfg = json.loads(Path(args.config).expanduser().read_text(encoding="utf-8"))
        roots = ["/"] if cfg.get("optInOnly") is False else [str(r) for r in cfg.get("optInPaths") or []]
        banks = {str(k): str(v) for k, v in (cfg.get("mapPathToBank") or {}).items()}
    except (OSError, ValueError, AttributeError) as exc:
        print(f"capture check: the plugin settings are unreadable ({type(exc).__name__}); nothing checked",
              file=sys.stderr)
        return 2
    if not roots:
        print("capture check: the plugin settings name no opted-in root; nothing checked", file=sys.stderr)
        return 2
    api, default_bank, key = _conn()
    now = time.time()
    since, settled = now - args.hours * 3600, now - args.settle_min * 60
    counts = dict.fromkeys(("interactive", "present", "missing", "headless", "headless_absent", "outside",
                            "empty", "fresh", "unknown"), 0)
    lines: list[str] = []
    projects = Path(args.projects).expanduser()
    for f in sorted(projects.glob("*/*.jsonl")):
        mtime = f.stat().st_mtime
        if mtime < since:
            continue
        if mtime > settled:
            counts["fresh"] += 1
            continue
        try:
            head = transcript_head(f)
        except OSError as exc:
            counts["unknown"] += 1
            lines.append(f"UNREADABLE {f.parent.name}/{f.name}: {type(exc).__name__}")
            continue
        if head is None:
            counts["empty"] += 1
            continue
        root = opted_root(head["cwd"], roots)
        if root is None:
            counts["outside"] += 1
            continue
        doc_id = f"conversation:{f.stem}"
        bank = banks.get(root) or default_bank
        headless = head["entrypoint"].startswith("sdk")
        try:
            _api("GET", f"{api}/v1/default/banks/{bank}/documents/{urllib.parse.quote(doc_id, safe='')}", key)
            there = True
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                counts["unknown"] += 1
                lines.append(f"UNKNOWN {doc_id}  bank {bank}: HTTP {exc.code}")
                continue
            there = False
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"capture check: the server did not answer ({type(exc).__name__}: {exc}); nothing more checked",
                  file=sys.stderr)
            return 2
        when = datetime.fromtimestamp(mtime, timezone.utc).isoformat(timespec="seconds")
        if headless:
            counts["headless"] += 1
            counts["headless_absent"] += not there
            lines.append(f"headless, {'absent by design' if not there else 'present'}: {doc_id}  {f.parent.name}  {when}")
            continue
        counts["interactive"] += 1
        counts["present" if there else "missing"] += 1
        if not there:
            lines.append(f"MISSING {doc_id}  bank {bank}  {f.parent.name}  {when}")
    c = counts
    print(f"capture check, transcripts modified in the last {args.hours:g} h and quiet for {args.settle_min:g} min: "
          f"{c['interactive']} interactive ({c['present']} present, {c['missing']} missing), "
          f"{c['headless']} headless ({c['headless_absent']} absent by design), "
          f"{c['outside']} outside the opted-in roots, {c['empty']} with no conversation, "
          f"{c['fresh']} still active, {c['unknown']} not checked")
    for line in sorted(lines):
        print(line)
    return 2 if c["unknown"] else 1 if c["missing"] else 0


# ============================================================
# Self-test: cleanup, mirror and tags, invented banks, no network
# ============================================================


@contextlib.contextmanager
def _patched(**names):
    """Swap module globals for one scenario, and put them back."""
    g = globals()
    saved = {k: g[k] for k in names if k in g}
    g.update(names)
    try:
        yield
    finally:
        for k in names:
            g.pop(k, None)
        g.update(saved)


class _FakeBank:
    """An invented bank: `GET .../documents` pages through `ids`, `DELETE` removes one.
    `fail_on` answers that id's DELETE with HTTP 500; `grow_after` adds an id after
    that many GETs, so the total moves while paging."""

    def __init__(self, ids, fail_on=(), grow_after=None, page=2, misreport_after=None, on_delete=None,
                 failed_ops=(), op_status=None):
        self.docs = [i if isinstance(i, dict) else {"id": i} for i in ids]
        self.fail_on, self.grow_after, self.page = set(fail_on), grow_after, page
        self.misreport_after = misreport_after  # report a total one higher, the pages themselves unchanged
        self.on_delete = on_delete  # called with each deleted id, after the DELETE
        self.failed_ops = list(failed_ops)  # what `operations?status=failed` lists
        self.op_status = dict(op_status or {})  # operation id -> its read; absent: completed
        self.gets, self.deletes, self.posts = 0, [], []

    def __call__(self, method, url, key, body=None):
        if method == "POST":  # a retain: the documents join the bank
            self.posts += [it["document_id"] for it in body["items"]]
            self.docs += [{"id": it["document_id"]} for it in body["items"]]
            return {"operation_id": f"op-{len(self.posts)}"}
        if method == "GET" and "/operations/" in url:  # one operation by id
            op = url.rsplit("/operations/", 1)[1]
            got = self.op_status.get(op, {"status": "completed"})
            if isinstance(got, Exception):
                raise got
            return {"operation_id": op, **got}
        if method == "GET" and "/operations?" in url:
            off = int(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["offset"][0])
            return {"operations": self.failed_ops[off: off + 100], "total": len(self.failed_ops)}
        if method == "GET" and "/documents/" in url:  # one document by id
            doc_id = urllib.parse.unquote(url.rsplit("/documents/", 1)[1])
            if doc_id not in {d["id"] for d in self.docs}:
                raise urllib.error.HTTPError(url, 404, "invented", None, None)
            return {"id": doc_id}
        if method == "GET":
            self.gets += 1
            if self.grow_after is not None and self.gets > self.grow_after:
                self.docs.append({"id": f"grown/{self.gets}.md"})
            off = int(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["offset"][0])
            bump = int(self.misreport_after is not None and self.gets > self.misreport_after)
            return {"items": self.docs[off: off + self.page], "total": len(self.docs) + bump}
        doc_id = urllib.parse.unquote(url.rsplit("/documents/", 1)[1])
        self.deletes.append(doc_id)
        if doc_id in self.fail_on:
            raise urllib.error.HTTPError(url, 500, "invented", None, None)
        if doc_id not in {d["id"] for d in self.docs}:
            raise urllib.error.HTTPError(url, 404, "invented", None, None)
        self.docs = [d for d in self.docs if d["id"] != doc_id]
        if self.on_delete:
            self.on_delete(doc_id)
        return {}


def _run(bank, root, files, ledger, argv, page=2, **extra):
    """`main(argv)` against the invented bank and tree; (exit code, ledger lines, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    names = {"_api": bank, "get_data_root": lambda: root, "_conn": lambda: ("http://invented.invalid", "main", "k"),
             "tracked": lambda r: sorted(files), "untracked": lambda r: [], "ledger_lines": lambda: list(ledger),
             "skip_prefixes": lambda: ("gone/",), "LIST_PAGE": page,
             "_auto_memory_prefix": lambda r: "invented-memory/", **extra}
    with _patched(**names), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = main(argv)
        except SystemExit as exc:  # argparse refusing the command line
            rc = exc.code
    lines = [json.loads(x) for x in out.getvalue().splitlines() if x.startswith("{")]
    return rc, lines, out.getvalue(), err.getvalue()


def _sent(path):
    return {"path": path, "sha256": "s", "operation_id": "op", "sent": "2026-01-01T00:00:00+00:00"}


def _fixture(tmp: Path):
    root = tmp / "data"
    (root / "keep").mkdir(parents=True)
    (root / "keep/untracked.md").write_text("on disk, not tracked", encoding="utf-8")
    files = {"keep/a.md", "keep/b.md"}
    ids = ["conversation:abc", "gitlog:main", "survey-baseline:123", "keep/a.md", "keep/b.md#part-02",
           "keep/untracked.md", {"id": "audit/1.md", "tags": ["workspace", "area:audit/1.md"]}, "audit/2.md",
           "audit/3.md", "gone/x.md", "stray.md",
           {"id": "repository-map", "tags": ["source:upload", "harness:claude-code"]},
           {"id": "initiative-marker-x", "tags": [], "document_metadata": {"relatedPageId": "kp-invented"}}]
    ledger = [_sent("keep/a.md"), _sent("audit/1.md"), _sent("audit/2.md"), _sent("audit/3.md"), _sent("gone/x.md")]
    return root, files, ids, ledger


def _st_plan_never_lists_sessions_gitlog_or_existing_files(tmp: Path) -> None:
    root, files, ids, ledger = _fixture(tmp)
    rc, _, out, _ = _run(_FakeBank(ids), root, files, ledger, ["cleanup", "plan"])
    assert rc == 0, out
    planned = [line.split()[1] for line in out.splitlines() if re.match(r"^\s+\d+  \S", line)]
    assert planned == ["audit/1.md", "audit/2.md", "audit/3.md", "gone/x.md", "stray.md"], planned
    assert "wave-skip.txt prefix gone/" in out and "never sent by hindsight-sync" in out, out
    assert "not a repository path (session, git history, plugin): 3" in out and "its file exists: 3" in out, out
    assert "tied to a knowledge page: 1" in out and "not this script's): 1" in out, out


def _st_incomplete_listing_plans_and_deletes_nothing(tmp: Path) -> None:
    root, files, ids, ledger = _fixture(tmp)
    plan = tmp / "plan.json"
    rc, _, _, err = _run(_FakeBank(ids, grow_after=1), root, files, ledger, ["cleanup", "plan", "--save", str(plan)])
    assert rc == 1 and "incomplete" in err and not plan.exists(), err
    # a total that moves between pages while the ids still add up to the first one
    rc, _, _, err = _run(_FakeBank(ids, misreport_after=1), root, files, ledger, ["cleanup", "plan"])
    assert rc == 1 and "incomplete" in err, err
    rc, _, _, _ = _run(_FakeBank(ids), root, files, ledger, ["cleanup", "plan", "--save", str(plan)])
    assert rc == 0 and plan.exists()
    bank = _FakeBank(ids, grow_after=1)
    rc, lines, _, err = _run(bank, root, files, ledger, ["cleanup", "apply", "--plan", str(plan), "--numbers", "1-3"])
    assert rc == 1 and bank.deletes == [] and lines == [] and "deleting nothing" in err, (bank.deletes, err)
    body = json.loads(plan.read_text(encoding="utf-8"))
    body["complete"] = False
    plan.write_text(json.dumps(body), encoding="utf-8")
    bank = _FakeBank(ids)
    rc, lines, _, err = _run(bank, root, files, ledger, ["cleanup", "apply", "--plan", str(plan), "--numbers", "1"])
    assert rc == 2 and bank.deletes == [] and lines == [], err


def _st_only_named_numbers_are_deleted(tmp: Path) -> None:
    root, files, ids, ledger = _fixture(tmp)
    plan = tmp / "plan.json"
    _run(_FakeBank(ids), root, files, ledger, ["cleanup", "plan", "--prefix", "audit/", "--save", str(plan)])
    bank = _FakeBank(ids)
    rc, lines, _, err = _run(bank, root, files, ledger, ["cleanup", "apply", "--plan", str(plan), "--numbers", "2"])
    assert rc == 0 and bank.deletes == ["audit/2.md"], (bank.deletes, err)
    assert [k for k in ("delete_intent", "deleted") if any(k in x for x in lines)] == ["delete_intent", "deleted"]
    assert lines[0]["path"] == "audit/2.md" and lines[1]["deleted"] == "audit/2.md", lines
    led = memory_plan.index_ledger(ledger + lines)
    assert led["audit/2.md"]["deleted"] == {"audit/2.md"}, "the mirror ledger reads the cleanup lines"
    bank = _FakeBank(ids)
    rc, _, _, _ = _run(bank, root, files, ledger, ["cleanup", "apply", "--plan", str(plan), "--numbers", "9"])
    assert rc == 2 and bank.deletes == [], "a number not in the plan deletes nothing"
    bank = _FakeBank(ids)
    rc, _, _, _ = _run(bank, root, files, ledger, ["cleanup", "apply", "--plan", str(plan)])
    assert rc == 2 and bank.deletes == [], "a plan with no numbers deletes nothing"


def _st_apply_rechecks_and_refuses_a_file_that_exists(tmp: Path) -> None:
    root, files, ids, ledger = _fixture(tmp)
    plan = tmp / "plan.json"
    _run(_FakeBank(ids), root, files, ledger, ["cleanup", "plan", "--prefix", "audit/", "--save", str(plan)])
    bank = _FakeBank(ids)
    rc, _, _, err = _run(bank, root, files | {"audit/1.md"}, ledger,
                         ["cleanup", "apply", "--plan", str(plan), "--numbers", "1-2"])
    assert rc == 0 and bank.deletes == ["audit/2.md"] and "refused" in err, (bank.deletes, err)
    bank = _FakeBank([i for i in ids if i != "audit/3.md"])
    rc, lines, _, err = _run(bank, root, files, ledger, ["cleanup", "apply", "--plan", str(plan), "--numbers", "3"])
    assert rc == 0 and bank.deletes == [] and lines == [] and "not in the bank now" in err, (bank.deletes, err)


def _st_limit_holds(tmp: Path) -> None:
    root, files, ids, ledger = _fixture(tmp)
    bank = _FakeBank(ids)
    rc, lines, _, err = _run(bank, root, files, ledger, ["cleanup", "apply", "--prefix", "audit/", "--limit", "2"])
    assert rc == 0 and bank.deletes == ["audit/1.md", "audit/2.md"] and "1 more left" in err, (bank.deletes, err)
    assert sum(1 for x in lines if "deleted" in x) == 2, lines
    bank = _FakeBank(ids)
    rc, _, _, _ = _run(bank, root, files, ledger, ["cleanup", "apply", "--prefix", "audit/", "--dry-run"])
    assert rc == 0 and bank.deletes == [], "a dry run deletes nothing"
    bank = _FakeBank(ids)
    rc, _, _, _ = _run(bank, root, files, ledger, ["cleanup", "apply", "--prefix", "conversation:"])
    assert rc == 2 and bank.deletes == [], "a prefix that is not a path deletes nothing"


def _st_failure_stops_the_rest_and_404_is_done(tmp: Path) -> None:
    root, files, ids, ledger = _fixture(tmp)
    bank = _FakeBank(ids, fail_on={"audit/2.md"})
    rc, lines, _, _ = _run(bank, root, files, ledger, ["cleanup", "apply", "--prefix", "audit/"])
    assert rc == 1 and bank.deletes == ["audit/1.md", "audit/2.md"], bank.deletes
    assert [next(k for k in ("delete_intent", "deleted", "delete_failed") if k in x) for x in lines] == [
        "delete_intent", "deleted", "delete_intent", "delete_failed"], lines
    gone = _FakeBank(ids)
    gone.docs = [d for d in gone.docs if d["id"] != "audit/1.md"]  # listed, then gone by the time DELETE runs
    with _patched(_api=gone), contextlib.redirect_stdout(io.StringIO()) as out, \
            contextlib.redirect_stderr(io.StringIO()):
        assert delete_recorded("http://invented.invalid", "main", "k", "audit/1.md", {"path": "audit/1.md"})
    rec = json.loads(out.getvalue())
    assert rec["deleted"] == "audit/1.md" and rec["absent"] is True, rec


def _push(bank, root, files, ledger, argv=(), state=None, extra=None):
    """`push --wave all` against the invented bank and tree, the registry replaced by
    `state` (empty by default) and a complete scan; nothing is read from the data root."""
    from unittest import mock
    key = root.parent / "key"
    key.write_text("k", encoding="utf-8")
    setting = {"HINDSIGHT_SYNC_API_URL": "http://invented.invalid", "HINDSIGHT_SYNC_KEY_FILE": str(key)}

    def last(_=None):
        seen = {}
        for rec in ledger:
            seen[rec["path"]] = rec
        return seen

    # an invented ledger is far under the 1 % floor of a real one; the fraction is lifted here
    # so `MAX_DELETES` alone caps a pass (the portions scenario asserts the real arithmetic)
    with mock.patch.object(vr, "registry_path", lambda: root.parent / "registry.jsonl"), \
            mock.patch.object(memory_plan, "MAX_FRACTION", 1.0), \
            mock.patch.object(doc_scan, "default_cache_path", lambda: root.parent / "scan-cache.json"):
        return _run(bank, root, files, ledger, ["push", "--wave", "all", "--pause", "0", *argv],
                    _setting=lambda n, d: setting.get(n, d), last_sent=last, registry_rels=lambda r: set(),
                    core_prefixes=lambda: (), last_prefixes=lambda: (),
                    prepare_memory=lambda *a, **k: (state or vr.State(), doc_scan.ScanResult(complete=True), None),
                    load_domain_labels=dict, ask_domains=lambda r, paths: {},
                    record_new_labels=lambda got: None, **(extra or {}))


def _mirror_fixture(tmp: Path):
    """A tree and a ledger where three sent files left: one deleted, one in two parts, one
    now under the `gone/` wave-skip prefix while still tracked. The rest must stay."""
    root = tmp / "data"
    for rel in ("keep/a.md", "keep/untracked.md", "gone/x.md"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(f"invented {rel}", encoding="utf-8")
    files = {"keep/a.md", "gone/x.md"}
    ledger = [_sent("keep/a.md"), _sent("keep/untracked.md"), _sent("gone/x.md"), _sent("moved/old.md"),
              {**_sent("moved/big.md"), "operation_ids": ["op1", "op2"]}, _sent("gitlog:main")]
    ids = ["conversation:abc", "gitlog:main", "stray.md", "keep/a.md", "keep/untracked.md", "gone/x.md",
           "moved/old.md", "moved/big.md#part-01", "moved/big.md#part-02"]
    return root, files, ids, ledger


def _st_mirror_deletes_sent_files_that_left_the_repository(tmp: Path) -> None:
    root, files, ids, ledger = _mirror_fixture(tmp)
    bank = _FakeBank(ids)
    rc, lines, _, err = _push(bank, root, files, ledger)
    assert rc == 0, err
    assert bank.deletes == ["gone/x.md", "moved/big.md#part-01", "moved/big.md#part-02", "moved/old.md"], (
        bank.deletes, err)
    kinds = [next(k for k in ("delete_intent", "deleted", "delete_failed", "sha256") if k in x) for x in lines]
    assert kinds == ["sha256"] + ["delete_intent", "deleted"] * 4, (kinds, err)
    assert all(x.get("mirror") for x in lines if "deleted" in x or "delete_intent" in x), lines
    led = memory_plan.index_ledger(ledger + lines)
    assert led["moved/big.md"]["deleted"] == {"moved/big.md#part-01", "moved/big.md#part-02"}
    rc, again, _, _ = _push(_FakeBank(ids), root, files, ledger + lines)
    assert rc == 0 and not any("delete_intent" in x for x in again), "a deleted id is never asked again"


def _st_mirror_never_selects_sessions_gitlog_bank_only_or_untracked_on_disk(tmp: Path) -> None:
    root, files, ids, ledger = _mirror_fixture(tmp)
    bank = _FakeBank(ids)
    _push(bank, root, files, ledger)
    for kept in ("conversation:abc", "gitlog:main", "stray.md", "keep/a.md", "keep/untracked.md"):
        assert kept not in bank.deletes, (kept, bank.deletes)
    # a member of a managed family goes by the family's rules, never by the mirror step: the
    # memory plan holds the first two, and nothing but `family_paths` the twin of a PDF gone
    # with its twin, which the plan cannot see once the twin is off the disk
    fam = vr.Family("f1", confirmed=True, actual="a1",
                    members={"a1": vr.Member("a1", "moved/old.md", "s"), "a2": vr.Member("a2", "moved/big.md", "t"),
                             "a3": vr.Member("a3", "moved/deck.pdf", "u")})
    bank = _FakeBank(ids + ["moved/deck-extract.md"])
    _push(bank, root, files, ledger + [_sent("moved/deck-extract.md")], state=vr.State(families={"f1": fam}))
    assert bank.deletes == ["gone/x.md"], bank.deletes
    # a file git still tracks is kept even while it is off the disk (an edit under way)
    bank = _FakeBank(ids)
    _push(bank, root, files | {"moved/old.md"}, ledger)
    assert "moved/old.md" not in bank.deletes and "moved/big.md#part-01" in bank.deletes, bank.deletes


def _st_mirror_portions_carry_over_to_the_next_passes(tmp: Path) -> None:
    from unittest import mock
    assert [mirror_cap(n) for n in (0, 50, 500, 12_000)] == [1, 1, 5, 20], "the guard's real constants"
    assert mirror_cap(12_000, already=15) == 5 and mirror_cap(12_000, already=30) == 0
    root = tmp / "data"
    (root / "keep").mkdir(parents=True)
    (root / "keep/a.md").write_text("invented", encoding="utf-8")
    gone = [f"left/{i:02d}.md" for i in range(7)]
    ledger = [_sent("keep/a.md")] + [_sent(p) for p in gone]
    bank = _FakeBank(["keep/a.md", *gone])
    passes, errs = [], []
    with mock.patch.object(memory_plan, "MAX_DELETES", 3), mock.patch.object(memory_plan, "MAX_FRACTION", 1.0):
        for _ in range(4):
            n = len(bank.deletes)
            rc, lines, _, err = _push(bank, root, {"keep/a.md"}, ledger)
            assert rc == 0 and "Waiting for the operator" not in err, err
            passes.append(bank.deletes[n:])
            errs.append(err)
            ledger = ledger + lines
    assert passes == [gone[:3], gone[3:6], gone[6:], []], passes
    assert "4 left for the next passes" in errs[0] and "left for" not in errs[2], errs


def _st_mirror_stops_on_failure_dry_run_named_and_a_file_that_came_back(tmp: Path) -> None:
    root, files, ids, ledger = _mirror_fixture(tmp)
    bank = _FakeBank(ids, fail_on={"moved/big.md#part-01"})
    rc, lines, _, _ = _push(bank, root, files, ledger)
    assert rc == 1 and bank.deletes == ["gone/x.md", "moved/big.md#part-01"], bank.deletes
    assert "delete_failed" in lines[-1] and "delete_intent" in lines[-2], lines
    bank = _FakeBank(ids)
    rc, lines, _, err = _push(bank, root, files, ledger, ["--dry-run"])
    assert rc == 0 and bank.deletes == [] and lines == [] and "mirror delete moved/old.md" in err, err
    bank = _FakeBank(ids)
    rc, _, _, _ = _push(bank, root, files, ledger, ["--path", "keep/a.md"])
    assert rc == 0 and bank.deletes == [], "a named push leaves the mirror step to the full pass"
    def back(_doc_id):  # the first DELETE lands while git brings a file back
        (root / "moved").mkdir(exist_ok=True)
        (root / "moved/big.md").write_text("back", encoding="utf-8")

    bank = _FakeBank(ids, on_delete=back)
    rc, lines, _, err = _push(bank, root, files, ledger)
    assert rc == 0 and bank.deletes == ["gone/x.md"] and "is back" in err, (bank.deletes, err)
    assert not any("delete_intent" in x and x["path"] == "moved/big.md" for x in lines), lines
    bank = _FakeBank(ids)
    rc, _, _, err = _push(bank, root, set(), ledger)
    assert rc == 0 and bank.deletes == [] and "no tracked file" in err, "an empty git listing deletes nothing"


def _st_personal_paths_are_tagged_personal_in_every_part(tmp: Path) -> None:
    root = tmp / "data"
    # a top-level personal directory is not exercised here: engine code may not spell that
    # data address at all, even joined (check-data-path-references.py --pins-check refuses it)
    for rel, text in (("life/personal/a.md", "x"), ("plain/c.md", "x"),
                      ("notes/personalia/d.md", "x"), ("life/personal/big.md", "line\n" * 100)):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    tags = {rel: [it["tags"] for it in items_for(root, rel, max_bytes=120)]
            for rel in ("life/personal/a.md", "plain/c.md", "notes/personalia/d.md",
                        "life/personal/big.md")}
    assert all("personal" in t for t in tags["life/personal/a.md"]), tags
    assert not any("personal" in t for t in tags["plain/c.md"] + tags["notes/personalia/d.md"]), tags
    assert len(tags["life/personal/big.md"]) > 1 and all("personal" in t for t in tags["life/personal/big.md"]), tags
    assert tags["plain/c.md"] == [["workspace", "area:plain/c.md"]], "any other path keeps its tags exactly"


def _write_dated(root: Path, rel: str, text: str, mtime_day: str = "2026-02-03") -> None:
    """An invented file whose mtime is noon UTC of `mtime_day`, so the fallback is known."""
    f = root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")
    t = datetime.fromisoformat(f"{mtime_day}T12:00:00+00:00").timestamp()
    os.utime(f, (t, t))


def _st_documents_carry_their_content_date_not_the_mtime(tmp: Path) -> None:
    """HOS-191. Before it, every one of these was sent with its mtime, 2026-02-03 here."""
    root = tmp / "data"
    cases = {  # path: (text, the day it must be sent with)
        "a/fm-updated.md": ("---\ncreated: 2025-01-01\nupdated: 2025-06-07\n---\nx\n", "2025-06-07"),
        "a/fm-last.md": ("---\nlast_updated: '2025-06-08T09:30:00'\ndate: 2025-01-01\n---\nx\n", "2025-06-08"),
        "a/fm-memory.md": ("---\nname: n\nmetadata:\n  modified: 2025-06-09T07:15:05.508Z\n  last_accessed: 2026-01-30\n"
                           "created: 2025-01-01\n---\nx\n", "2025-06-09"),
        "a/fm-created.md": ("---\ncreated: 2025-06-10\n---\nx\n", "2025-06-10"),
        "a/fm-future-then-date.md": ("---\nupdated: 2999-01-01\ndate: 2025-06-11\n---\nx\n", "2025-06-11"),
        "a/fm-garbage-then-line.md": ("---\nupdated: soon\n---\n**Last Updated:** 2025-06-12\n", "2025-06-12"),
        "a/line.md": ("# Title\n\n> Last updated: 2025-06-13\n", "2025-06-13"),
        "a/settings.yaml": ("name: x\nlast_updated: \"2025-06-14\"\n", "2025-06-14"),
        "a/2025-06-15_meeting-notes.md": ("no date inside\n", "2025-06-15"),
        "a/2025-13-40_not-a-date.md": ("no date inside\n", "2026-02-03"),
        "runs/2025-06-16-sweep/2025-06-01-old/report.md": ("no date inside\n", "2025-06-01"),
        "runs/2025-06-16-sweep/report.md": ("no date inside\n", "2025-06-16"),
        "a/plain.md": ("no date anywhere, and `Last Updated: {today}` is code\n", "2026-02-03"),
        "a/future-name-2999-01-01.md": ("x\n", "2026-02-03"),
    }
    for rel, (text, _) in cases.items():
        _write_dated(root, rel, text)
    got = {rel: items_for(root, rel)[0]["timestamp"][:10] for rel in cases}
    want = {rel: day for rel, (_, day) in cases.items()}
    assert got == want, {k: (got[k], want[k]) for k in cases if got[k] != want[k]}
    big = "---\nupdated: 2025-06-17\n---\n" + "line\n" * 100
    _write_dated(root, "a/big.md", big)
    stamps = {it["timestamp"] for it in items_for(root, "a/big.md", max_bytes=120)}
    assert stamps == {"2025-06-17T00:00:00+00:00"}, stamps


def _st_a_bump_that_restores_mtime_is_still_sent(tmp: Path) -> None:
    """HOS-191, MEASURED 2026-10-03: memory records kept size and whole-second mtime
    across a `memory_touch` bump, so `changed()` skipped the hash and the bank kept the
    old bytes. Both ledger shapes must see the bump; an untouched file must not be read."""
    from scripts.utils import memory_touch
    root = tmp / "data"
    mem = root / "mem"
    rel = "mem/a-record.md"
    _write_dated(root, rel, "---\nname: a\nmetadata:\n  access_count: 3\n  last_accessed: 2026-01-01\n---\nbody\n")
    before = _ledger_meta(root / rel)
    time.sleep(1.1)  # a real send follows the write; the old line's proof is a send time to the second
    sent = datetime.now(timezone.utc).isoformat(timespec="seconds")
    old_line = {"path": rel, "sha256": before["sha256"], "bytes": before["bytes"], "mtime": before["mtime"],
                "sent": sent}
    new_line = {**before, "path": rel, "sent": sent}

    def no_read(_):
        raise AssertionError("an untouched file was read")

    with _patched(sha256=no_read):
        assert not changed(root, rel, {rel: old_line}) and not changed(root, rel, {rel: new_line})
    memory_touch.touch_file(rel.rsplit("/", 1)[1], mem, "2026-01-02")
    st = (root / rel).stat()
    assert (st.st_size, int(st.st_mtime)) == (before["bytes"], before["mtime"]), "the repro needs equal size and mtime"
    assert sha256(root / rel) != before["sha256"], "the bump changed the bytes"
    assert changed(root, rel, {rel: old_line}), "an older ledger line missed the bump"
    assert changed(root, rel, {rel: new_line}), "a ledger line with ctime_ns missed the bump"


def _st_only_files_whose_date_moved_are_sent_again(tmp: Path) -> None:
    """HOS-191: the first pass re-sends a file whose content date differs from the mtime
    day it was sent with, and no other unchanged file; the lines it writes carry `ts`, and
    a second pass over them sends nothing."""
    root = tmp / "data"
    texts = {"k/dated.md": "---\nupdated: 2025-06-07\n---\nx\n",  # was sent as 2026-02-03: moves
             "k/same-day.md": "---\nupdated: 2026-02-03\n---\nx\n",  # its date is the mtime's day
             "k/undated.md": "nothing\n",  # falls back to the mtime, as before
             "k/2025-06-08_named.md": "nothing\n"}  # its name dates it
    for rel, text in texts.items():
        _write_dated(root, rel, text)
    sent = (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat(timespec="seconds")
    ledger = [{"path": rel, "sent": sent, "operation_id": "op",
               **{k: v for k, v in _ledger_meta(root / rel).items() if k != "ctime_ns"}} for rel in texts]
    bank = _FakeBank([])
    rc, lines, _, err = _push(bank, root, set(texts), ledger)
    assert rc == 0, err
    assert sorted(bank.posts) == ["k/2025-06-08_named.md", "k/dated.md"], bank.posts
    by = {rec["path"]: rec for rec in lines if "sha256" in rec}
    assert by["k/dated.md"]["ts"] == "2025-06-07T00:00:00+00:00" and "ctime_ns" in by["k/dated.md"], by
    again = _FakeBank([])
    rc, _, _, err = _push(again, root, set(texts), ledger + list(by.values()))
    assert rc == 0 and again.posts == [], (again.posts, err)


def _st_admitted_archive_prefixes_are_sent_and_nothing_else_is(tmp: Path) -> None:
    """HOS-193: a `wave-admit.txt` prefix lifts the archive rule for that prefix only;
    another archive, and a non-text file under the admitted prefix, stay out."""
    root = tmp / "data"
    files = {"n/prompt-archive/a.md", "n/handoff-archive/b.md", "n/prompt-archive/c.pdf", "n/plain.md"}
    for rel in files:
        _write_dated(root, rel, "x\n")
    admit = {"wave-admit.txt": ("n/prompt-archive/",)}
    bank = _FakeBank([])
    rc, _, _, err = _push(bank, root, files, [], extra={"_prefixes": lambda name: admit.get(name, ())})
    assert rc == 0, err
    assert sorted(bank.posts) == ["n/plain.md", "n/prompt-archive/a.md"], bank.posts
    assert excluded_why("n/handoff-archive/b.md", set()) is not None


def _st_an_earlier_shape_leaves_through_the_mirror(tmp: Path) -> None:
    """HOS-193: the whole document beside the parts sent since, and parts a file no longer
    has, are deleted with ledger lines; a file uploaded this pass, one whose ledger and
    split disagree, and a dry run delete nothing of their own."""
    root = tmp / "data"
    long = "".join(f"line {i}\n" for i in range(60))
    texts = {"k/big.md": long, "k/small.md": "short\n", "k/misrecorded.md": long, "k/changed.md": long}
    for rel, text in texts.items():
        _write_dated(root, rel, text)
    parts = len(split_text(sendable_text(root / "k/big.md"), 120))
    assert parts > 1, parts

    def up(rel, n, fresh=True):
        m = _ledger_meta(root / rel) if fresh else {"sha256": "old", "bytes": 1, "mtime": 1, "ctime_ns": 1}
        rec = {"path": rel, "operation_id": "op", "sent": "2026-02-04T00:00:00+00:00",
               "ts": document_date(root / rel, rel)[0].isoformat(), **m}
        return {**rec, "operation_ids": [f"op{i}" for i in range(n)]} if n > 1 else rec

    ledger = [up("k/big.md", 1, False), up("k/big.md", parts),  # whole, then parts
              up("k/small.md", 3, False), up("k/small.md", 1),  # parts, then whole
              up("k/misrecorded.md", parts, False), up("k/misrecorded.md", 1),  # the ledger says whole, the file splits
              up("k/changed.md", 1, False), up("k/changed.md", parts, False)]  # bytes differ: uploaded this pass
    ids = ["k/big.md", "k/small.md", "k/misrecorded.md", "k/changed.md"] + \
          [f"{p}#part-{i:02d}" for p in ("k/big.md", "k/small.md", "k/misrecorded.md", "k/changed.md")
           for i in range(1, parts + 1)]
    dry = _FakeBank(ids)
    rc, _, _, err = _push(dry, root, set(texts), ledger, ["--max-bytes", "120", "--dry-run"])
    assert rc == 0 and dry.deletes == [] and dry.posts == [], (dry.deletes, err)
    assert "mirror delete k/big.md [an earlier shape" in err, err
    bank = _FakeBank(ids)
    rc, lines, _, err = _push(bank, root, set(texts), ledger, ["--max-bytes", "120"])
    assert rc == 0, err
    want = ["k/big.md", "k/small.md#part-01", "k/small.md#part-02", "k/small.md#part-03"]
    assert sorted(bank.deletes) == want, bank.deletes
    assert sorted(r["deleted"] for r in lines if "deleted" in r) == want, lines
    assert set(bank.posts) == {f"k/changed.md#part-{i:02d}" for i in range(1, parts + 1)}, bank.posts


def _st_work_folders_are_never_sent_and_leave_through_the_mirror(tmp: Path) -> None:
    """HOS-195: nothing inside a `_work/` folder is sent, not by the wave, not by name, not
    through a `wave-admit.txt` prefix; a document already sent from one, still tracked and
    on disk, leaves the bank with ledger lines; a name that only contains `_work` is sent."""
    root = tmp / "data"
    files = {"d/report.md", "d/_work/draft.md", "d/_work/deep/notes.md", "d/_workshop/a.md",
             "d/my_work/b.md", "n/prompt-archive/_work/c.md", "d/_work.md"}
    for rel in files:
        _write_dated(root, rel, "x\n")
    admit = {"wave-admit.txt": ("n/prompt-archive/",)}
    extra = {"_prefixes": lambda name: admit.get(name, ())}
    bank = _FakeBank([])
    rc, _, _, err = _push(bank, root, files, [], extra=extra)
    assert rc == 0, err
    assert sorted(bank.posts) == ["d/_work.md", "d/_workshop/a.md", "d/my_work/b.md", "d/report.md"], bank.posts
    bank = _FakeBank([])
    rc, _, out, err = _push(bank, root, files, [], ["--path", "d/_work/draft.md"], extra=extra)
    assert bank.posts == [] and "_work/" in out + err, (bank.posts, out, err)
    sent = ["d/_work/draft.md", "d/_work/deep/notes.md", "d/report.md"]
    ledger = [{**_sent(p), **_ledger_meta(root / p), "ts": document_date(root / p, p)[0].isoformat()}
              for p in sent]
    bank = _FakeBank(list(sent))
    rc, lines, _, err = _push(bank, root, files, ledger, extra=extra)
    assert rc == 0, err
    assert sorted(bank.deletes) == ["d/_work/deep/notes.md", "d/_work/draft.md"], (bank.deletes, err)
    assert all(x.get("mirror") == WORK_REASON for x in lines if "delete_intent" in x or "deleted" in x), lines
    assert sorted(x["deleted"] for x in lines if "deleted" in x) == sorted(bank.deletes), lines


def _st_status_sees_failures_on_the_ledgers_own_operations(tmp: Path) -> None:
    """HOS-182: a failure recorded on the operation the ledger holds (the batch the POST
    returned, a parent with no `document_id`) is reported and requeued, though the failed
    listing with `exclude_parents` never shows it; a failure older than the latest send by
    more than the grace, a completed send, a requeue line and a deletion line are not;
    a child failure from the listing still counts."""
    sent = "2026-02-01T00:00:00+00:00"
    after, before = "2026-02-01T00:00:05+00:00", "2026-01-31T23:00:00+00:00"
    ledger = [{**_sent("a/batch.md"), "operation_id": "op-batch", "sent": sent},
              {**_sent("a/ok.md"), "operation_id": "op-ok", "sent": sent},
              {**_sent("a/stale.md"), "operation_id": "op-stale", "sent": sent},
              {**_sent("a/parts.md"), "operation_id": "op-ok", "operation_ids": ["op-p2", "op-ok"], "sent": sent},
              {**_sent("a/child.md"), "operation_id": "op-ok", "sent": sent},
              {"path": "a/again.md", "requeue": True, "sent": sent},
              {"path": "a/gone.md", "deleted": "a/gone.md"}]
    single = {"op-batch": {"status": "failed", "created_at": after, "error_message": "wall limit"},
              "op-ok": {"status": "completed", "created_at": after},
              "op-stale": {"status": "failed", "created_at": before, "error_message": "old"},
              "op-p2": {"status": "failed", "created_at": after, "error_message": "extraction"}}
    listed = [{"id": "op-child", "document_id": "a/child.md#part-02", "created_at": after, "error_message": "child"}]

    def api(method, url, key, body=None):
        assert method == "GET", (method, url)
        if "/operations?" in url:  # the listing a parent never reaches with exclude_parents=true
            return {"operations": [] if int(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["offset"][0])
                    else listed, "total": len(listed)}
        return {"operation_id": url.rsplit("/", 1)[1], **single[url.rsplit("/", 1)[1]]}

    last = {}
    for rec in ledger:
        last[rec["path"]] = rec
    (tmp / "key").write_text("k", encoding="utf-8")
    fake = {"last_sent": lambda: dict(last),
            "_setting": lambda name, default: str(tmp / "key") if name == "HINDSIGHT_SYNC_KEY_FILE" else default}
    rc, lines, _, err = _run(api, tmp, set(), ledger, ["status", "--requeue-failed"], **fake)
    assert rc == 0, err
    assert sorted(x["path"] for x in lines) == ["a/batch.md", "a/child.md", "a/parts.md"], (lines, err)
    assert all(x["requeue"] is True for x in lines), lines
    assert {x["path"]: x["reason"] for x in lines}["a/batch.md"] == "wall limit", lines
    assert "failed 3" in err and "completed 1" in err, err
    rc, lines, _, err = _run(api, tmp, set(), ledger, ["status"], **fake)
    assert rc == 0 and lines == [] and err.count("failed on the server:") == 3, (lines, err)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _st_untracked_files_git_does_not_ignore_are_sent_and_mirrored(tmp: Path) -> None:
    """HOS-200 part 1. Before it only `git ls-files` was read, so a new file waited for a
    commit (MEASURED 2026-10-04: 22 untracked `.md` files in the data repository). A real
    git tree here: the file git ignores and the filtered ones stay out, the untracked one
    goes, stays while on disk, and leaves the bank once it is deleted."""
    root = tmp / "data"
    for rel in ("k/tracked.md", "k/new.md", "k/ignored.md", "k/_work/draft.md", "k/new.pdf"):
        _write_dated(root, rel, f"invented {rel}\n")
    (root / ".gitignore").write_text("k/ignored.md\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "k/tracked.md", ".gitignore")
    real = {"tracked": globals()["tracked"], "untracked": globals().get("untracked", lambda r: [])}
    bank = _FakeBank([])
    rc, lines, _, err = _push(bank, root, set(), [], extra=real)
    assert rc == 0, err
    assert sorted(bank.posts) == ["k/new.md", "k/tracked.md"], (bank.posts, err)
    named = _FakeBank([])
    rc, _, _, err = _push(named, root, set(), [], ["--path", "k/new.md", "--path", "k/ignored.md"], extra=real)
    assert rc == 2 and named.posts == ["k/new.md"] and "k/ignored.md" in err, (named.posts, err)
    bank = _FakeBank(["k/new.md", "k/tracked.md"])
    rc, _, _, err = _push(bank, root, set(), lines, extra=real)
    assert rc == 0 and bank.deletes == [] and bank.posts == [], (bank.deletes, bank.posts, err)
    (root / "k/new.md").unlink()
    rc, gone, _, err = _push(bank, root, set(), lines, extra=real)
    assert rc == 0 and bank.deletes == ["k/new.md"], (bank.deletes, err)
    assert [x.get("deleted") for x in gone if "deleted" in x] == ["k/new.md"] and \
        any("delete_intent" in x for x in gone), gone


def _st_machine_written_memory_records_stay_on_disk_and_leave_the_bank(tmp: Path) -> None:
    """HOS-200 part 2, operator decision, 2026-10-05: "Keep all of its records on disk only, remove them
    from Hindsight." (translated) A memory record whose frontmatter says the local model wrote it is never
    sent, and one already sent leaves through the mirror; no file is touched. Before it
    all four records under the memory prefix were sent."""
    root = tmp / "data"
    recs = {"mem/by-model.md": "---\nname: a\nmetadata:\n  type: feedback\n  written_by: cognee-distil\n---\nx\n",
            "mem/by-distil.md": "---\nname: b\nwritten_by: memory-distil\n---\nx\n",
            "mem/by-helm.md": "---\nname: c\nmetadata:\n  written_by: helm\n---\nx\n",
            "mem/plain.md": "no frontmatter; written_by: cognee-distil is only quoted here\n",
            "notes/elsewhere.md": "---\nwritten_by: cognee-distil\n---\noutside the memory records\n"}
    for rel, text in recs.items():
        _write_dated(root, rel, text)
    extra = {"_auto_memory_prefix": lambda r: "mem/"}
    bank = _FakeBank([])
    rc, _, _, err = _push(bank, root, set(recs), [], extra=extra)
    assert rc == 0, err
    assert sorted(bank.posts) == ["mem/by-helm.md", "mem/plain.md", "notes/elsewhere.md"], (bank.posts, err)
    bank = _FakeBank([])
    _push(bank, root, set(recs), [], ["--path", "mem/by-model.md"], extra=extra)
    assert bank.posts == [], bank.posts
    sent = sorted(recs)
    ledger = [{**_sent(p), **_ledger_meta(root / p), "ts": document_date(root / p, p)[0].isoformat()} for p in sent]
    before = {p: (root / p).read_bytes() for p in recs}
    bank = _FakeBank(list(sent))
    rc, lines, _, err = _push(bank, root, set(recs), ledger, extra=extra)
    assert rc == 0 and bank.posts == [], (bank.posts, err)
    assert sorted(bank.deletes) == ["mem/by-distil.md", "mem/by-model.md"], (bank.deletes, err)
    assert all("machine-written" in str(x.get("mirror")) for x in lines if "delete_intent" in x or "deleted" in x), lines
    assert before == {p: (root / p).read_bytes() for p in recs}, "a file on disk was touched"
    again = _FakeBank([])
    rc, more, _, _ = _push(again, root, set(recs), ledger + lines, extra=extra)
    assert rc == 0 and again.posts == [] and not any("delete_intent" in x for x in more), (again.posts, more)


def _st_failed_extractions_are_sent_again_a_bounded_number_of_times(tmp: Path) -> None:
    """HOS-200 part 3. Before it a failure stayed failed until someone ran `status
    --requeue-failed` by hand (41 on 2026-10-04). Now each full pass reads the failed
    operations once, sends again what failed after its latest send, ahead of the queue,
    and gives up after `FAILED_RESEND_LIMIT` resends; a dry run and a named push send none."""
    root = tmp / "data"
    rels = ["f/batch.md", "f/child.md", "f/poison.md", "f/stale.md", "f/ok.md", "f/new.md"]
    for rel in rels:
        _write_dated(root, rel, f"invented {rel}\n")
    sent, after, before = "2026-02-01T00:00:00+00:00", "2026-02-01T00:00:05+00:00", "2026-01-31T23:00:00+00:00"

    def line(rel, op, **kw):
        return {"path": rel, "operation_id": op, "sent": sent, **_ledger_meta(root / rel),
                "ts": document_date(root / rel, rel)[0].isoformat(), **kw}

    ledger = [line("f/batch.md", "op-batch"), line("f/child.md", "op-ok"), line("f/poison.md", "op-poison", retry=2),
              line("f/stale.md", "op-stale"), line("f/ok.md", "op-ok")]
    failed = [{"id": "op-batch", "operation_type": "batch_retain", "status": "failed", "document_id": None, "created_at": after,
               "error_message": "wall limit"},
              {"id": "op-child-2", "operation_type": "retain", "status": "failed", "document_id": "f/child.md#part-02",
               "created_at": after, "error_message": "chunk timeout"},
              {"id": "op-poison", "operation_type": "batch_retain", "status": "failed", "document_id": None, "created_at": after,
               "error_message": "wall limit"},
              {"id": "op-stale", "operation_type": "batch_retain", "status": "failed", "document_id": None, "created_at": before,
               "error_message": "an earlier version"},
              {"id": "op-mm", "operation_type": "refresh_mental_model", "status": "failed", "document_id": None, "created_at": after}]
    files = set(rels)
    dry = _FakeBank([], failed_ops=failed)
    rc, _, _, err = _push(dry, root, files, ledger, ["--dry-run"])
    assert rc == 0 and dry.posts == [], (dry.posts, err)
    assert "send again after a failed extraction: f/batch.md" in err and "f/child.md" in err, err
    assert "f/poison.md" in err and "given up" in err and "f/stale.md" not in err, err
    bank = _FakeBank([], failed_ops=failed)
    rc, lines, _, err = _push(bank, root, files, ledger, ["--limit", "2", "--batch", "1"])
    assert rc == 0 and sorted(bank.posts) == ["f/batch.md", "f/child.md"], (bank.posts, err)
    by = {x["path"]: x for x in lines if "sha256" in x}
    assert by["f/batch.md"]["retry"] == 1 and by["f/child.md"]["retry"] == 1, by
    bank = _FakeBank([], failed_ops=failed)
    rc, _, _, err = _push(bank, root, files, ledger + lines)
    assert rc == 0 and bank.posts == ["f/new.md"], ("an older failure is not sent again", bank.posts, err)
    now = datetime.now(timezone.utc).isoformat()
    again = failed + [{"id": by["f/batch.md"]["operation_id"], "operation_type": "batch_retain", "status": "failed",
                       "document_id": None, "created_at": now, "error_message": "wall limit again"}]
    bank = _FakeBank([], failed_ops=again)
    rc, more, _, err = _push(bank, root, files, ledger + lines + [line("f/new.md", "op-new")])
    assert rc == 0 and bank.posts == ["f/batch.md"], (bank.posts, err)
    assert [x["retry"] for x in more if x.get("path") == "f/batch.md"] == [2], more
    bank = _FakeBank([], failed_ops=failed)
    rc, _, _, err = _push(bank, root, files, ledger, ["--path", "f/ok.md"])
    assert rc == 0 and bank.posts == [], ("a named push sends what it names", bank.posts, err)
    assert FAILED_RESEND_LIMIT == 2


def _st_a_send_still_in_flight_is_never_deleted(tmp: Path) -> None:
    """MEASURED 2026-10-12: the mirror deleted documents whose retain was still running and
    the late retain created them again. Every delete path defers such an id: mirror, family
    and `cleanup apply`; a deferred id writes no line and takes no place under a cap; a
    pending child, or a status that cannot be read, defers too; once settled it goes."""
    from unittest import mock
    root, files, ids, ledger = _mirror_fixture(tmp)
    slow = {"op1": {"status": "processing"}}
    with mock.patch.object(memory_plan, "MAX_DELETES", 2):
        bank = _FakeBank(ids, op_status=slow)
        rc, lines, _, err = _push(bank, root, files, ledger)
    assert rc == 0 and bank.deletes == ["gone/x.md", "moved/old.md"], (bank.deletes, err)
    assert "mirror defer moved/big.md#part-01" in err and "operation op1 not settled (processing)" in err, err
    assert not any("big.md" in json.dumps(x) for x in lines if "delete_intent" in x or "deleted" in x), lines
    assert "2 deferred until their send settles" in err, err
    rc, _, _, err = _push(_FakeBank(ids, op_status=slow), root, files, ledger, ["--dry-run"])
    assert rc == 0 and "mirror defer moved/big.md#part-02" in err, err
    child = {"op": {"status": "completed", "child_operations": [{"status": "completed"}, {"status": "pending"}]}}
    bank = _FakeBank(ids, op_status=child)
    rc, _, _, err = _push(bank, root, files, ledger)
    assert rc == 0 and bank.deletes == ["moved/big.md#part-01", "moved/big.md#part-02"], (bank.deletes, err)
    broken = {"op1": urllib.error.HTTPError("u", 500, "invented", None, None)}
    bank = _FakeBank(ids, op_status=broken)
    rc, _, _, err = _push(bank, root, files, ledger)
    assert rc == 0 and "big.md" not in " ".join(bank.deletes) and "unreadable (HTTP 500)" in err, (bank.deletes, err)
    bank = _FakeBank(ids, op_status={"op1": {"status": "failed"}})
    rc, _, _, err = _push(bank, root, files, ledger)
    assert rc == 0 and "moved/big.md#part-01" in bank.deletes, "a failed send writes nothing more: it goes"
    # cleanup apply: a deferred id takes no place under --limit, and writes no line
    root, files, ids, ledger = _fixture(tmp / "c")
    ledger = [{**r, "operation_id": "op-slow"} if r["path"] == "audit/1.md" else r for r in ledger]
    bank = _FakeBank(ids, op_status={"op-slow": {"status": "pending"}})
    rc, lines, _, err = _run(bank, root, files, ledger, ["cleanup", "apply", "--prefix", "audit/", "--limit", "1"])
    assert rc == 0 and bank.deletes == ["audit/2.md"] and "defer audit/1.md" in err, (bank.deletes, err)
    assert not any(x.get("path") == "audit/1.md" for x in lines), lines
    # family deletions: deferred before the daily cap, so the next id takes the place
    plan = memory_plan.Plan(delete=["h/1.md", "h/2.md"], delete_path={"h/1.md": "h/1.md", "h/2.md": "h/2.md"})
    sends = [{**_sent("h/1.md"), "operation_id": "op-slow"}, _sent("h/2.md")]
    with _patched(_api=_FakeBank([], op_status={"op-slow": {"status": "pending"}})), \
            mock.patch.object(memory_plan, "plan", lambda *a, **k: plan), \
            contextlib.redirect_stderr(io.StringIO()) as e:
        check = SendCheck("http://invented.invalid", "main", "k", sends)
        got = memory_plan.cap_daily(family_plan(vr.State(), {}, None, root, True, check), used=0, cap=1)
    assert got.delete == ["h/2.md"] and "defer delete h/1.md" in e.getvalue(), (got.delete, e.getvalue())


def _st_a_moved_file_leaves_in_the_same_pass_and_a_lookalike_does_not(tmp: Path) -> None:
    """2026-10-13, for the bulk move: a path that left whose last sent content is the settled,
    unchanged last send of a path still here has MOVED; all its ids go this pass, outside the
    portion, on ledger lines naming the new path. A lookalike proves no move: content that
    changed before the move, content changed on disk since the new send, a new send still in
    flight, a new path this pass uploads (the last two are pending moves, see the next
    scenario). Several new paths: one that proves is enough."""
    from unittest import mock
    root = tmp / "data"

    def put(rel, text):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def sent(rel, sha, op="op", **kw):
        return {**_sent(rel), "sha256": sha, "operation_id": op, **kw}

    a, b, c, d = (put(f"new/{n}.md", f"invented {n}") for n in "abcd")
    e = put("new/e1.md", "invented e")
    put("new/e2.md", "invented e")
    put("new/f.md", "invented f, edited after its send")
    files = {"new/a.md", "new/b.md", "new/c.md", "new/d.md", "new/e1.md", "new/e2.md", "new/f.md", "keep.md"}
    put("keep.md", "invented keep")
    ledger = [
        sent("new/a.md", a), sent("old/a.md", a),                               # a move
        sent("new/b.md", b, operation_ids=["ob1", "ob2"]),                     # a move in parts
        sent("old/b.md", b, operation_ids=["oo1", "oo2"]),
        sent("new/c.md", c), sent("old/c.md", "other content"),                 # changed before the move
        sent("new/d.md", d, op="op-slow"), sent("old/d.md", d),                 # new send in flight
        sent("new/e1.md", e, op="op-slow"), sent("new/e2.md", e), sent("old/e.md", e),  # one of two proves
        sent("new/f.md", "invented f"), sent("old/f.md", "invented f"),         # changed on disk since
        sent("gone/1.md", "x1"), sent("gone/2.md", "x2"),                       # ordinary deletions
    ]
    ids = ["new/a.md", "old/a.md", "new/b.md#part-01", "new/b.md#part-02", "old/b.md#part-01",
           "old/b.md#part-02", "new/c.md", "old/c.md", "new/d.md", "old/d.md", "new/e1.md", "new/e2.md",
           "old/e.md", "new/f.md", "old/f.md", "gone/1.md", "gone/2.md"]
    slow = {"op-slow": {"status": "processing"}}
    moved = {"old/a.md", "old/b.md#part-01", "old/b.md#part-02", "old/e.md"}
    with mock.patch.object(memory_plan, "MAX_DELETES", 1):
        bank = _FakeBank(ids, op_status=slow)
        rc, lines, _, err = _push(bank, root, files, ledger, ["--limit", "0"],
                                  extra={"needs_send": lambda *a: False})
        assert rc == 0, err
        assert set(bank.deletes[:4]) == moved and len(bank.deletes) == 5, (bank.deletes, err)
        assert bank.deletes[4] in {"gone/1.md", "gone/2.md", "old/c.md", "old/d.md", "old/f.md"}, bank.deletes
        mv = [x for x in lines if x.get("mirror") == MOVE_REASON]
        assert {x.get("deleted") for x in mv if "deleted" in x} == moved, lines
        assert {(x["path"], x["moved_to"]) for x in mv} == {
            ("old/a.md", "new/a.md"), ("old/b.md", "new/b.md"), ("old/e.md", "new/e2.md")}, mv
        assert all("moved_to" not in x for x in lines if x.get("mirror") != MOVE_REASON), lines
        assert "4 moved deleted" in err and "outside the portion" in err, err
        # the next pass: what moved is gone from the ledger's view, the portion goes on
        led2 = ledger + lines
        bank2 = _FakeBank([i for i in ids if i not in bank.deletes], op_status=slow)
        rc, lines2, _, err = _push(bank2, root, files, led2, extra={"needs_send": lambda *a: False})
        assert rc == 0 and len(bank2.deletes) == 1 and not set(bank2.deletes) & moved, (bank2.deletes, err)
        assert "moved deleted" not in err, err
        # dry run: prints the moves, deletes and writes nothing
        bank3 = _FakeBank(ids, op_status=slow)
        rc, lines3, _, err = _push(bank3, root, files, ledger, ["--dry-run"], extra={"needs_send": lambda *a: False})
        assert rc == 0 and bank3.deletes == [] and lines3 == [], (bank3.deletes, lines3)
        assert "mirror move old/a.md -> new/a.md (dry run)" in err, err
        # once the slow send settles, old/d.md is a move too
        bank4 = _FakeBank(ids)
        rc, lines4, _, err = _push(bank4, root, files, ledger, extra={"needs_send": lambda *a: False})
        assert rc == 0 and "old/d.md" in bank4.deletes[:5], (bank4.deletes, err)
        # a new path this pass uploads proves nothing: new/a.md resent, old/a.md waits its turn
        bank5 = _FakeBank(ids)
        rc, lines5, _, err = _push(bank5, root, files, ledger,
                                   extra={"needs_send": lambda r, p, *a: p == "new/a.md"})
        assert rc == 0 and not any(x.get("path") == "old/a.md" and x.get("mirror") == MOVE_REASON
                                   for x in lines5), lines5
        # the old id's own send still in flight defers it, move or not
        led6 = [sent("old/a.md", a, op="op-slow") if x["path"] == "old/a.md" else x for x in ledger]
        bank6 = _FakeBank(ids, op_status=slow)
        rc, _, _, err = _push(bank6, root, files, led6, extra={"needs_send": lambda *a: False})
        assert rc == 0 and "old/a.md" not in bank6.deletes and "mirror defer old/a.md" in err, (bank6.deletes, err)


def _st_a_pending_move_waits_and_empty_content_proves_no_move(tmp: Path) -> None:
    """2026-10-13, a real-data dry run of a bulk move planned old documents into the
    portion while their content was only being uploaded under the new paths, and called two
    unrelated empty files a move. A path that left whose content is on disk under a path
    queued for sending (this pass, or behind --limit) or whose send has not completed is a
    PENDING move: printed, no ledger line, no place in the portion, and a move once the new
    send completes (a FAILED send is settled, never proof). Content nowhere else, or only
    under a path that does not belong (wave-skip), is an ordinary deletion. Empty or
    whitespace-only content neither proves nor pends."""
    from unittest import mock
    root = tmp / "data"

    def put(rel, text):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def sent(rel, sha, op="op", **kw):
        return {**_sent(rel), "sha256": sha, "operation_id": op, "bytes": len((root / rel).read_bytes())
                if (root / rel).exists() else kw.pop("n", 1), **kw}

    p, q = put("new/p.md", "invented p"), put("new/q.md", "invented q")
    g = put("gone/g.md", "invented g")  # under the wave-skip prefix: does not belong
    z = put("new/z.txt", "")
    w = put("new/w.txt", " \n\t\n")
    put("new/z2.txt", "")
    files = {"new/p.md", "new/q.md", "gone/g.md", "new/z.txt", "new/w.txt", "new/z2.txt"}
    ledger = [sent("old/p.md", p, n=10), sent("old/q.md", q, n=10), sent("old/g.md", g, n=10),
              sent("old/x.md", "content nowhere else"),
              sent("new/z.txt", z), sent("old/z.txt", z, n=0),       # empty: settled, same sha
              sent("new/w.txt", w), sent("old/w.txt", w, n=5),       # whitespace only
              sent("old/z2.txt", z, n=0)]                            # empty, new/z2.txt queued
    ids = ["old/p.md", "old/q.md", "old/g.md", "old/x.md", "new/z.txt", "old/z.txt", "new/w.txt",
           "old/w.txt", "old/z2.txt"]
    unsent = {"new/p.md", "new/q.md", "new/z2.txt"}
    queued = {"needs_send": lambda r, path, *a: path in unsent}
    with mock.patch.object(memory_plan, "MAX_DELETES", 20):
        # new/p.md and new/q.md queued, --limit 1 sends one: both old paths pend, nothing of
        # them is deleted or written, and the portion still takes the ordinary deletions
        # gone/g.md's own send in flight: a path that does not belong makes no move pending
        slow = {"op-slow": {"status": "processing"}}
        bank = _FakeBank(ids + ["gone/g.md"], op_status=slow)
        rc, lines, _, err = _push(bank, root, files, ledger + [sent("gone/g.md", g, op="op-slow")],
                                  ["--limit", "1"], extra=queued)
        assert rc == 0, err
        assert "mirror pending-move old/p.md -> new/p.md: the new path is queued" in err, err
        assert "mirror pending-move old/q.md -> new/q.md: the new path is queued" in err, err
        assert not {"old/p.md", "old/q.md"} & set(bank.deletes), (bank.deletes, err)
        assert not any(x.get("path") in ("old/p.md", "old/q.md") for x in lines if "sha256" not in x), lines
        assert set(bank.deletes) == {"old/g.md", "old/x.md", "old/z.txt", "old/w.txt", "old/z2.txt"}, (
            bank.deletes, err)
        assert not any(x.get("mirror") == MOVE_REASON for x in lines), lines
        assert "pending-move old/z" not in err and "pending-move old/w" not in err, err
        # the portion counts no pending id: one place, and it goes to an ordinary deletion
        with mock.patch.object(memory_plan, "MAX_DELETES", 1):
            bank = _FakeBank(ids)
            rc, _, _, err = _push(bank, root, files, ledger, extra=queued)
            assert rc == 0 and len(bank.deletes) == 1 and bank.deletes[0] not in {"old/p.md", "old/q.md"}, (
                bank.deletes, err)
            assert "at most 1 deleted" in err and "5 document(s) whose file left" in err, err
        # the next pass: new/p.md sent, its send in flight; still pending, still not deleted
        led2 = ledger + [sent("new/p.md", p, op="op-slow"), sent("new/q.md", q)]
        bank = _FakeBank(ids, op_status={"op-slow": {"status": "processing"}})
        rc, lines, _, err = _push(bank, root, files, led2, extra={"needs_send": lambda *a: False})
        assert rc == 0 and "old/p.md" not in bank.deletes, (bank.deletes, err)
        assert "pending-move old/p.md -> new/p.md: the new path's send has not completed" in err, err
        assert "mirror move" not in err and "old/q.md" in bank.deletes, (bank.deletes, err)
        assert [x["moved_to"] for x in lines if x.get("path") == "old/q.md" and "deleted" in x] == ["new/q.md"], lines
        # settled: a proven move, outside the portion
        bank = _FakeBank(ids)
        rc, lines, _, err = _push(bank, root, files, led2, ["--dry-run"], extra={"needs_send": lambda *a: False})
        assert rc == 0 and "mirror move old/p.md -> new/p.md (dry run)" in err and "pending-move" not in err, err
        # the new send FAILED: settled, yet no proof; old/p.md stays pending, not deleted,
        # no ledger line, no place in the portion; once a resend completes it is a move
        led3 = ledger + [sent("new/p.md", p, op="op-bad"), sent("new/q.md", q, op="op-kid")]
        bad = {"op-bad": {"status": "failed"},
               "op-kid": {"status": "completed", "child_operations": [{"status": "failed"}]}}
        bank = _FakeBank(ids, op_status=bad)
        rc, lines, _, err = _push(bank, root, files, led3, extra={"needs_send": lambda *a: False})
        assert rc == 0 and not {"old/p.md", "old/q.md"} & set(bank.deletes), (bank.deletes, err)
        assert "pending-move old/p.md -> new/p.md: the new path's send has not completed: its last send " \
               "failed: operation op-bad" in err, err
        assert "pending-move old/q.md -> new/q.md" in err and "op-kid" in err, err
        assert not any(x.get("path") in ("old/p.md", "old/q.md") for x in lines), lines
        assert not any(x.get("mirror") == MOVE_REASON for x in lines), lines
        led4 = led3 + [sent("new/p.md", p, op="op-again")]
        bank = _FakeBank(ids, op_status=bad)
        rc, lines, _, err = _push(bank, root, files, led4, extra={"needs_send": lambda *a: False})
        assert rc == 0 and "old/p.md" in bank.deletes and "old/q.md" not in bank.deletes, (bank.deletes, err)
        assert [x["moved_to"] for x in lines if x.get("path") == "old/p.md" and "deleted" in x] == ["new/p.md"], lines
        # empty and whitespace-only content: never a move, the ordinary rule with its reason
        bank = _FakeBank(ids)
        rc, lines, _, err = _push(bank, root, files, led2, extra={"needs_send": lambda *a: False})
        for old in ("old/z.txt", "old/w.txt", "old/z2.txt"):
            got = [x for x in lines if x.get("path") == old and "deleted" in x]
            assert got and got[0]["mirror"] == LEFT_REASON and "moved_to" not in got[0], (old, lines)
        assert trivial_content(root / "new/z.txt") and trivial_content(root / "new/w.txt")
        assert not trivial_content(root / "new/p.md")


def _st_capture_check_reports_interactive_sessions_missing_from_the_bank(tmp: Path) -> None:
    """HOS-200 part 4. Every interactive transcript under an opted-in root, modified in the
    window and settled, must have `conversation:<session>` in its bank; a headless run
    (`sdk-cli`) is absent by design and is not a miss. Invented roots and sessions."""
    projects, inroot = tmp / "projects", "/invented/root"
    cfg = tmp / "coding-agent.json"
    cfg.write_text(json.dumps({"apiToken": "never-printed-token", "optInPaths": [inroot],
                               "mapPathToBank": {inroot: "main"}}), encoding="utf-8")
    now = time.time()

    def transcript(slug, sid, cwd, entry, age_h, user=True):
        f = projects / slug / f"{sid}.jsonl"
        f.parent.mkdir(parents=True, exist_ok=True)
        recs = [{"type": "queue-operation", "operation": "enqueue"}]
        if user:
            recs.append({"type": "user", "cwd": cwd, "entrypoint": entry, "sessionId": sid,
                         "message": {"role": "user", "content": "invented"}})
        f.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
        os.utime(f, (now - age_h * 3600,) * 2)

    transcript("-a", "s-present", inroot + "/yard/x", "cli", 2)
    transcript("-a", "s-missing", inroot, "cli", 3)
    transcript("-a", "s-headless", inroot, "sdk-cli", 4)
    transcript("-b", "s-outside", "/invented/elsewhere", "cli", 5)
    transcript("-c", "s-sibling", "/invented/rootless", "cli", 5)
    transcript("-a", "s-old", inroot, "cli", 30)
    transcript("-a", "s-fresh", inroot, "cli", 0.01)
    transcript("-a", "s-empty", inroot, "cli", 6, user=False)
    transcript("-a/s-present/subagents", "agent-x", inroot, "cli", 2)
    argv = ["capture-check", "--config", str(cfg), "--projects", str(projects)]
    rc, _, out, err = _run(_FakeBank(["conversation:s-present"]), tmp, set(), [], argv)
    assert rc == 1, (rc, out, err)
    missing = [x.split()[1] for x in out.splitlines() if x.startswith("MISSING")]
    assert missing == ["conversation:s-missing"], (missing, out)
    assert "headless, absent by design: conversation:s-headless" in out, out
    assert "2 interactive" in out and "1 present" in out, out
    assert "never-printed-token" not in out + err
    for sid in ("s-outside", "s-sibling", "s-old", "s-fresh", "s-empty", "agent-x"):
        assert sid not in out, (sid, out)
    rc, _, out, err = _run(_FakeBank(["conversation:s-present", "conversation:s-missing"]), tmp, set(), [], argv)
    assert rc == 0 and "MISSING" not in out, (rc, out, err)


SELF_TESTS = [
    _st_plan_never_lists_sessions_gitlog_or_existing_files,
    _st_incomplete_listing_plans_and_deletes_nothing,
    _st_only_named_numbers_are_deleted,
    _st_apply_rechecks_and_refuses_a_file_that_exists,
    _st_limit_holds,
    _st_failure_stops_the_rest_and_404_is_done,
    _st_mirror_deletes_sent_files_that_left_the_repository,
    _st_mirror_never_selects_sessions_gitlog_bank_only_or_untracked_on_disk,
    _st_mirror_portions_carry_over_to_the_next_passes,
    _st_mirror_stops_on_failure_dry_run_named_and_a_file_that_came_back,
    _st_personal_paths_are_tagged_personal_in_every_part,
    _st_documents_carry_their_content_date_not_the_mtime,
    _st_a_bump_that_restores_mtime_is_still_sent,
    _st_only_files_whose_date_moved_are_sent_again,
    _st_admitted_archive_prefixes_are_sent_and_nothing_else_is,
    _st_an_earlier_shape_leaves_through_the_mirror,
    _st_work_folders_are_never_sent_and_leave_through_the_mirror,
    _st_status_sees_failures_on_the_ledgers_own_operations,
    _st_untracked_files_git_does_not_ignore_are_sent_and_mirrored,
    _st_machine_written_memory_records_stay_on_disk_and_leave_the_bank,
    _st_failed_extractions_are_sent_again_a_bounded_number_of_times,
    _st_capture_check_reports_interactive_sessions_missing_from_the_bank,
    _st_a_send_still_in_flight_is_never_deleted,
    _st_a_moved_file_leaves_in_the_same_pass_and_a_lookalike_does_not,
    _st_a_pending_move_waits_and_empty_content_proves_no_move,
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("plan", cmd_plan), ("push", cmd_push)):
        s = sub.add_parser(name)
        s.add_argument("--wave", choices=("core", "rest", "all"), default="core")
        s.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES,
                       help="the largest document; a longer file is sent in parts of this size")
        s.add_argument("--max-file-bytes", type=int, default=DEFAULT_MAX_FILE_BYTES,
                       help="a file whose text is longer than this is not sent")
        s.set_defaults(fn=fn)
        if name == "push":
            s.add_argument("--limit", type=int, default=0)
            s.add_argument("--sample", action="store_true", help="spread --limit evenly across the wave")
            s.add_argument("--batch", type=int, default=10)
            s.add_argument("--pause", type=float, default=2.0, help="seconds between batches")
            s.add_argument("--dry-run", action="store_true")
            s.add_argument("--allow-deletes", action="store_true",
                           help="delete planned version-family documents from the bank; off by default, and the timer "
                                "never passes it (the mirror step needs no flag)")
            s.add_argument("--detect", action="store_true",
                           help="run the version detector on this pass's scan before planning and append its "
                                "proposals to the registry (a dry run only prints them); the timer passes it")
            s.add_argument("--max-pending", type=int, default=0,
                           help="send nothing while the server already has this many pending operations")
            s.add_argument("--path", action="append", metavar="P",
                           help="send only this data-relative file or directory, ahead of the queue (repeatable); "
                                "every filter still applies, --wave, --limit and --sample do not")
            s.add_argument("--paths", metavar="FILE",
                           help="as --path, one path per line read from FILE (blank lines and # comments skipped)")
    st = sub.add_parser("status")
    st.add_argument("--requeue-failed", action="store_true",
                    help="print a ledger line for each file whose latest send failed, so push sends it again")
    st.set_defaults(fn=cmd_status)
    c = sub.add_parser("cleanup", help="documents of the bank with no source file; deleted only on the operator's word")
    csub = c.add_subparsers(dest="cleanup_cmd", required=True)
    cp = csub.add_parser("plan", help="list them, numbered, with the reason; deletes nothing")
    cp.add_argument("--prefix", default="", help="only ids starting with this repository path prefix")
    cp.add_argument("--save", metavar="FILE", help="write the numbered plan here, for `cleanup apply --plan`")
    cp.set_defaults(fn=cmd_cleanup_plan)
    ca = csub.add_parser("apply", help="delete the documents the operator named; stdout is ledger lines")
    ca.add_argument("--plan", metavar="FILE", help="a plan saved by `cleanup plan --save`")
    ca.add_argument("--numbers", metavar="N", help="plan numbers, e.g. 1-20,25")
    ca.add_argument("--prefix", default="", help="every planned id under this prefix, named explicitly")
    ca.add_argument("--limit", type=int, default=CLEANUP_DEFAULT_LIMIT,
                    help=f"at most this many deletions per run (default {CLEANUP_DEFAULT_LIMIT})")
    ca.add_argument("--dry-run", action="store_true", help="print what would be deleted; delete nothing")
    ca.set_defaults(fn=cmd_cleanup_apply)
    b = sub.add_parser("blocked", help="family deletions the mass-delete guard blocked; deleted only on a confirmation")
    bsub = b.add_subparsers(dest="blocked_cmd", required=True)
    bsub.add_parser("list", help="list them with their family and the confirm token; deletes nothing").set_defaults(
        fn=cmd_blocked_list)
    bd = bsub.add_parser("delete", help="delete the listed set within the daily cap; stdout is ledger lines")
    bd.add_argument("--confirm", required=True, metavar="N:H", help="the token `blocked list` printed")
    bd.add_argument("--dry-run", action="store_true", help="print what would be deleted; delete nothing")
    bd.set_defaults(fn=cmd_blocked_delete)
    cc = sub.add_parser("capture-check", help="every interactive session in the opted-in roots has its conversation "
                                              "document in the bank; exit 1 on a miss")
    cc.add_argument("--hours", type=float, default=24.0, help="transcripts modified in this many hours (default 24)")
    cc.add_argument("--settle-min", type=float, default=15.0,
                    help="skip a transcript modified this recently: its session may not have stopped yet")
    cc.add_argument("--config", default=CAPTURE_CONFIG, help="the plugin's settings file")
    cc.add_argument("--projects", default=CAPTURE_PROJECTS, help="Claude Code's per-project transcript directory")
    cc.set_defaults(fn=cmd_capture_check)
    sub.add_parser("self-test", help="the cleanup, mirror and tag scenarios on an invented bank; no network").set_defaults(
        fn=cmd_self_test)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
