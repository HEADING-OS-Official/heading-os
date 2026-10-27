#!/usr/bin/env python3
"""Keep a daily copy on disk of what lives ONLY in the Hindsight bank, and answer from it when Hindsight does not.

    hindsight-snapshot.py take [--out DIR]          # export, read-only against the server; atomic swap
    hindsight-snapshot.py status [--out DIR]        # the current snapshot's date and counts
    hindsight-snapshot.py query "<text>" [--json] [--top-k N] [--timeout S]   # the snapshot alone, via the local index
    hindsight-snapshot.py lookup "<text>" [--json] [--top-k N]                # Hindsight first, the snapshot when it does not answer
    hindsight-snapshot.py restore-check ARCHIVE [--scratch DIR]  # unpack an archived snapshot, compare files and bytes to its manifest
    hindsight-snapshot.py self-test                 # invented bank on a loopback server; no real server, no real data

Why it exists. Operator decision, 2026-10-05 (HOS-203; master instruction in the prompt
archive, slug `hindsight-primary-memory`): Hindsight is the main memory. Work documents
stay files in git and Hindsight mirrors them, so a lost server loses none of them. What a
lost server WOULD take with it is what exists nowhere else, and that is what this writes:

  - observations: the consolidated memory units (`memories/list?type=observation`);
  - mental models, with their content, and directives, active or not;
  - the knowledge-base export bundle (pages, their index, their change logs);
  - documents with no file behind them: every document of the bank except those mirrored
    from a file of the data repository. The split is `hindsight-sync.py`'s own, reused and
    not rewritten: a document is MIRRORED exactly when `owner_mark()` names no other owner
    and `no_source_reason()` finds its file git-tracked or on disk. Everything else
    (conversations, git history, the plugin's uploads, `memory:` ids written by
    `hindsight-remember.py`, and path-shaped ids whose file is gone) is copied.

NOT copied: the documents mirrored from files (the files are the copy), and the world and
experience facts Hindsight extracted from any document (re-extracted from the text on a
restore; what is copied is the text they came from).

Where. `runtime:hindsight-snapshot/<kind>` in the data repository, `SNAPSHOT_ADDR` below.
`hindsight-sync.py` never sends anything under `runtime-junk/` (`excluded_why`), so the
snapshot cannot loop back into the bank it copies. The local memory index indexes it
through the layer `hindsight-snapshot` of `config/memory-index.yaml`, which is the one
layer allowed under the runtime store, and it sits in a collection of its own so an
ordinary `/recall` never mixes it in.

Layout, under the snapshot directory:

    current/manifest.json              written LAST; its presence is what makes a snapshot whole
    current/observations/<stem>.md     one per observation
    current/documents/<stem>.pNNN.md   one or more parts per Hindsight-only document
    current/mental-models/<stem>.pNNN.md
    current/directives/<stem>.md
    current/knowledge-base/...         the export bundle, verbatim

A text over `PART_CHARS` is cut into parts at line ends, so the local index (which embeds
at most twelve 700-character chunks of one file) reaches all of it. Every file is
frontmatter, the line `---`, then the body; `read_item()` returns the body exactly, and
the parts of one item joined in order are its text byte for byte.

Atomicity. A run builds `next-<pid>/` beside `current/`, under a lock. A file whose bytes
equal the one in `current/` is hard-linked rather than rewritten, so its mtime, which is
what the index's incremental build compares, does not move. Only after every listing came
back complete (each page carried the same `total` and the distinct ids add up to it, as in
`hindsight-sync.py list_documents`) and every count was checked is `manifest.json`
written, `current/` renamed aside and `next-<pid>/` renamed in. Any failure removes
`next-<pid>/` and leaves the last good snapshot untouched; a crash between the two renames
is repaired at the start of the next run.

Settings are `hindsight-sync.py`'s (`HINDSIGHT_SYNC_API_URL`, `HINDSIGHT_SYNC_BANK`,
`HINDSIGHT_SYNC_KEY_FILE`). Every request this makes to the server is a GET, except the
`recall` POST of `lookup`, which reads too.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.utils.data_path import data_path, data_write_dir  # noqa: E402
from scripts.utils.paths import get_data_root  # noqa: E402
from scripts.utils.user_agent import user_agent  # noqa: E402
from scripts.utils.loopback_json_server import serve as serve_loopback  # noqa: E402

#: The registry address, spelled as a literal at each `data_path` call so the leak guard
#: can read it; this copy names it for the self-test. `cache` is gitignored in the data
#: repository and `state` is tracked; which one this should be is the operator's call on the
#: measured size and churn (HOS-203 closing note), and the answer changes the three
#: spellings and the `hindsight-snapshot` layer glob in `config/memory-index.yaml`.
SNAPSHOT_ADDR = "runtime:hindsight-snapshot/cache"
#: The memory-index collection that holds the snapshot and nothing else.
COLLECTION = "hindsight-snapshot"
CURRENT = "current"
MANIFEST = "manifest.json"
FORMAT = 1
PART_CHARS = 8000
PAGE = 1000
FETCH_WORKERS = 8
KINDS = ("observations", "documents", "mental-models", "directives", "knowledge-base")
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"
MEMORY_INDEX = ROOT / "scripts" / "memory-index.py"


class Incomplete(RuntimeError):
    """A listing or a fetch did not come back whole; nothing is replaced."""


# ============================================================
# The sync script's own logic, loaded rather than copied
# ============================================================

_SYNC = None


def sync_module():
    """`scripts/hindsight-sync.py` as a module: its connection settings, its document
    listing and its mirrored-or-not split are used as they are, never re-written here."""
    global _SYNC
    if _SYNC is None:
        spec = importlib.util.spec_from_file_location("hindsight_sync_for_snapshot",
                                                      ROOT / "scripts" / "hindsight-sync.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SYNC = mod
    return _SYNC


def is_mirrored(doc: dict, files: set[str], led: dict[str, dict], root: Path, skip: tuple[str, ...]) -> bool:
    """True when the document is the bank's copy of a data-repository file that is still there."""
    sync = sync_module()
    return sync.owner_mark(doc) is None and sync.no_source_reason(doc, led, files, root, skip) is None


# ============================================================
# Reading the bank
# ============================================================


def _get(api: str, path: str, key: str) -> dict:
    return sync_module()._api("GET", f"{api}{path}", key)


def list_paged(api: str, key: str, path: str, page: int = PAGE, attempts: int = 3) -> list[dict]:
    """Every item of a paged listing, or `Incomplete`. Counted complete only when every
    page carried the same `total` and the distinct ids add up to it."""
    why = "no attempt made"
    sep = "&" if "?" in path else "?"
    for _ in range(attempts):
        items: list[dict] = []
        total = None
        offset = 0
        moved = False
        while True:
            body = _get(api, f"{path}{sep}limit={page}&offset={offset}", key)
            got, t = body.get("items"), body.get("total")
            if not isinstance(got, list) or not isinstance(t, int):
                raise Incomplete(f"{path}: the listing carried no items or no total")
            if total is None:
                total = t
            elif t != total:
                why, moved = f"{path}: the bank changed while paging ({total} -> {t})", True
                break
            items += [x for x in got if isinstance(x, dict)]
            offset += len(got)
            if not got or offset >= t:
                break
        if moved:
            continue
        ids = {str(x.get("id") or "") for x in items}
        if "" in ids or len(ids) != len(items) or len(items) != total:
            why = f"{path}: {len(ids)} distinct ids against a reported total of {total}"
            continue
        return items
    raise Incomplete(why)


def list_whole(api: str, key: str, path: str, attempts: int = 3) -> list[dict]:
    """Every item of a listing in ONE response, or `Incomplete`.

    For a listing that moves faster than it can be paged: MEASURED 2026-10-05 on the live
    bank, the observation total moved during every one of three paged passes of 34 pages
    (consolidation writes continuously), while a single request with a limit above the
    total came back whole (items equal to its own total, all ids distinct) in 31 s. The
    total is asked first, the limit set above it with room to grow, and the answer is
    accepted only when it carries every item its own total names."""
    sep = "&" if "?" in path else "?"
    why = "no attempt made"
    for _ in range(attempts):
        first = _get(api, f"{path}{sep}limit=1&offset=0", key).get("total")
        if not isinstance(first, int):
            raise Incomplete(f"{path}: the listing carried no total")
        body = _get(api, f"{path}{sep}limit={first + max(1000, first // 10)}&offset=0", key)
        items, total = body.get("items"), body.get("total")
        if not isinstance(items, list) or not isinstance(total, int):
            raise Incomplete(f"{path}: the listing carried no items or no total")
        ids = {str(x.get("id") or "") for x in items if isinstance(x, dict)}
        if len(items) == total and len(ids) == total and "" not in ids:
            return items
        why = f"{path}: {len(ids)} distinct ids in one answer against its own total of {total}"
    raise Incomplete(why)


def fetch_document(api: str, bank: str, key: str, doc_id: str) -> dict:
    quoted = urllib.parse.quote(doc_id, safe="")
    body = _get(api, f"/v1/default/banks/{bank}/documents/{quoted}", key)
    if str(body.get("id")) != doc_id or not isinstance(body.get("original_text"), str):
        raise Incomplete(f"document {doc_id!r} came back without its text")
    return body


# ============================================================
# Rendering items as files
# ============================================================


def stem(item_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", item_id).strip("._-")[:80] or "item"
    return f"{slug}-{hashlib.sha256(item_id.encode('utf-8')).hexdigest()[:10]}"


def split_parts(text: str, size: int = PART_CHARS) -> list[str]:
    """Cut at line ends where one falls in the second half of a window; joined, the parts are `text`."""
    if len(text) <= size:
        return [text]
    parts, i = [], 0
    while i < len(text):
        j = min(len(text), i + size)
        if j < len(text):
            k = text.rfind("\n", i + size // 2, j)
            if k > i:
                j = k + 1
        parts.append(text[i:j])
        i = j
    return parts


def _day(value) -> str:
    return str(value or "")[:10]


def render(meta: dict, body: str) -> bytes:
    """Frontmatter of JSON scalars (valid YAML), then `---`, then the body verbatim."""
    head = "".join(f"{k}: {json.dumps(v, ensure_ascii=False)}\n" for k, v in meta.items())
    return f"---\n{head}---\n{body}".encode("utf-8")


def read_item(path: Path) -> tuple[dict, str]:
    """The frontmatter and the exact body of one snapshot file."""
    return parse_item(path.read_bytes())


def parse_item(data: bytes) -> tuple[dict, str]:
    text = data.decode("utf-8")
    if not text.startswith("---\n"):
        raise ValueError("not a snapshot item")
    end = text.index("\n---\n", 3)
    meta = {}
    for line in text[4:end].splitlines():
        k, _, v = line.partition(": ")
        meta[k] = json.loads(v)
    return meta, text[end + 5:]


def _title(text: str, fallback: str) -> str:
    one = " ".join(text.split())
    return (one[:90] + "...") if len(one) > 90 else (one or fallback)


def observation_files(obs: dict) -> dict[str, bytes]:
    text = str(obs.get("text") or "")
    meta = {"title": _title(text, str(obs["id"])), "type": "hindsight-observation",
            "created": _day(obs.get("occurred_start") or obs.get("mentioned_at") or obs.get("date")),
            "updated": _day(obs.get("updated_at") or obs.get("mentioned_at")),
            "hindsight_kind": "observation", "hindsight_id": str(obs["id"]),
            "tags": list(obs.get("tags") or []),
            "source_memories": len(obs.get("source_memory_ids") or [])}
    return {f"observations/{stem(str(obs['id']))}.md": render(meta, text)}


def parted_files(kind: str, item_id: str, title: str, text: str, extra: dict) -> dict[str, bytes]:
    parts = split_parts(text)
    out = {}
    for n, part in enumerate(parts, 1):
        meta = {"title": title if len(parts) == 1 else f"{title} (part {n} of {len(parts)})",
                **extra, "hindsight_id": item_id, "part": n, "parts": len(parts)}
        out[f"{kind}/{stem(item_id)}.p{n:03d}.md"] = render(meta, part)
    return out


def document_files(doc: dict) -> dict[str, bytes]:
    return parted_files("documents", str(doc["id"]), str(doc["id"]), doc["original_text"],
                        {"type": "hindsight-document", "created": _day(doc.get("created_at")),
                         "updated": _day(doc.get("updated_at")), "hindsight_kind": "document",
                         "tags": list(doc.get("tags") or []),
                         "document_metadata": doc.get("document_metadata") or {}})


def mental_model_files(mm: dict) -> dict[str, bytes]:
    body = f"Source query: {mm.get('source_query') or ''}\n\n{mm.get('content') or ''}"
    return parted_files("mental-models", str(mm["id"]), str(mm.get("name") or mm["id"]), body,
                        {"type": "hindsight-mental-model", "created": _day(mm.get("created_at")),
                         "updated": _day(mm.get("last_refreshed_at") or mm.get("created_at")),
                         "hindsight_kind": "mental-model", "tags": list(mm.get("tags") or [])})


def directive_files(d: dict) -> dict[str, bytes]:
    meta = {"title": str(d.get("name") or d["id"]), "type": "hindsight-directive",
            "created": _day(d.get("created_at")), "updated": _day(d.get("updated_at")),
            "hindsight_kind": "directive", "hindsight_id": str(d["id"]),
            "priority": d.get("priority"), "is_active": d.get("is_active"),
            "tags": list(d.get("tags") or [])}
    return {f"directives/{stem(str(d['id']))}.md": render(meta, str(d.get("content") or ""))}


def bundle_files(files: list) -> dict[str, bytes]:
    """The export bundle verbatim, each path kept inside `knowledge-base/`; a log goes to `logs/`
    so the index layer (non-recursive there) never embeds a change log."""
    out = {}
    for f in files:
        rel = Path(str(f.get("path") or ""))
        if not rel.parts or rel.is_absolute() or ".." in rel.parts:
            raise Incomplete(f"the knowledge-base bundle named an unsafe path {str(rel)!r}")
        sub = "logs/" if rel.name.endswith(".log.md") else ""
        out[f"knowledge-base/{sub}{rel.as_posix()}"] = str(f.get("content") or "").encode("utf-8")
    return out


# ============================================================
# take
# ============================================================


def collect(api: str, bank: str, key: str, root: Path) -> tuple[dict[str, bytes], dict]:
    """Every file of a snapshot and its manifest, or `Incomplete`. Reads only."""
    sync = sync_module()
    files, led, skip = sync._cleanup_inputs(root)
    docs, why = sync.list_documents(api, bank, key)
    if docs is None:
        raise Incomplete(f"documents: {why}")
    only = sorted(i for i, d in docs.items() if not is_mirrored(d, files, led, root, skip))
    with ThreadPoolExecutor(FETCH_WORKERS) as pool:
        fetched = list(pool.map(lambda i: fetch_document(api, bank, key, i), only))
    observations = list_whole(api, key, f"/v1/default/banks/{bank}/memories/list?type=observation")
    models = list_paged(api, key, f"/v1/default/banks/{bank}/mental-models?detail=content", page=100)
    directives = list_paged(api, key, f"/v1/default/banks/{bank}/directives?active_only=false", page=100)
    bundle = _get(api, f"/v1/default/banks/{bank}/knowledge-base/export", key).get("files")
    if not isinstance(bundle, list):
        raise Incomplete("the knowledge-base export carried no files")
    out: dict[str, bytes] = {}
    for o in observations:
        out.update(observation_files(o))
    for d in fetched:
        out.update(document_files(d))
    for m in models:
        out.update(mental_model_files(m))
    for d in directives:
        out.update(directive_files(d))
    out.update(bundle_files(bundle))
    counts = {"observations": len(observations), "documents": len(fetched), "mental-models": len(models),
              "directives": len(directives), "knowledge-base-files": len(bundle)}
    manifest = {"format": FORMAT, "bank": bank,
                "taken_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "counts": counts, "documents_listed": len(docs),
                "documents_mirrored_from_files": len(docs) - len(fetched), "files": len(out),
                "bytes": sum(len(b) for b in out.values()), "sha256": tree_digest(out)}
    return out, manifest


def tree_digest(out: dict[str, bytes]) -> str:
    """One sha256 over every path and its bytes in path order, so a restore can tell a copy
    whose files kept their count and sizes but not their content."""
    h = hashlib.sha256()
    for rel in sorted(out):
        h.update(rel.encode("utf-8") + b"\0" + str(len(out[rel])).encode() + b"\0" + out[rel])
    return h.hexdigest()


def check(out: dict[str, bytes], manifest: dict) -> None:
    """Refuse a snapshot whose files do not add up to its own counts."""
    c = manifest["counts"]
    have = dict.fromkeys(KINDS, 0)
    ids = {"documents": set(), "mental-models": set()}
    for rel, data in out.items():
        kind = rel.split("/", 1)[0]
        have[kind] = have.get(kind, 0) + 1
        if kind in ids:
            ids[kind].add(parse_item(data)[0]["hindsight_id"])
    expect = {"observations": c["observations"], "directives": c["directives"],
              "knowledge-base": c["knowledge-base-files"]}
    for kind, n in expect.items():
        if have.get(kind, 0) != n:
            raise Incomplete(f"{kind}: {have.get(kind, 0)} files for {n} items")
    for kind in ids:
        if len(ids[kind]) != c[kind]:
            raise Incomplete(f"{kind}: {len(ids[kind])} items written for {c[kind]} listed")


@contextlib.contextmanager
def locked(base: Path):
    fh = open(base / ".take.lock", "w")  # noqa: SIM115 - held for the whole block
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise Incomplete("another snapshot run holds the lock") from None
    try:
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def repair(base: Path) -> None:
    """Undo what a crash left: a `current/` renamed aside and never replaced comes back,
    and a half-built `next-*` is removed. Runs under the lock."""
    cur = base / CURRENT
    olds = sorted(base.glob(".old-*"), key=lambda p: p.stat().st_mtime)
    if not cur.exists() and olds:
        os.rename(olds[-1], cur)
        olds = olds[:-1]
    for p in olds + list(base.glob("next-*")):
        shutil.rmtree(p, ignore_errors=False)


def write_tree(base: Path, out: dict[str, bytes], manifest: dict) -> dict:
    """Build `next-<pid>/`, then swap it in. Returns what changed, for the churn figure."""
    cur, nxt = base / CURRENT, base / f"next-{os.getpid()}"
    nxt.mkdir()
    linked = written = 0
    try:
        for rel, data in sorted(out.items()):
            dest = nxt / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            old = cur / rel
            if old.is_file() and old.stat().st_size == len(data) and old.read_bytes() == data:
                os.link(old, dest)
                linked += 1
            else:
                dest.write_bytes(data)
                written += 1
        before = {p.relative_to(cur).as_posix() for p in cur.rglob("*") if p.is_file()} if cur.is_dir() else set()
        removed = len(before - set(out) - {MANIFEST})
        manifest = {**manifest, "unchanged_files": linked, "new_or_changed_files": written, "removed_files": removed}
        tmp = nxt / (MANIFEST + ".tmp")
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        os.replace(tmp, nxt / MANIFEST)
    except BaseException:
        shutil.rmtree(nxt, ignore_errors=True)
        raise
    aside = base / f".old-{os.getpid()}"
    if cur.exists():
        os.rename(cur, aside)
    os.rename(nxt, cur)
    if aside.exists():
        shutil.rmtree(aside)
    return manifest


def snapshot_base(out: str | None, create: bool = False) -> Path:
    if out:
        p = Path(out).expanduser().resolve()
        if create:
            p.mkdir(parents=True, exist_ok=True)
        return p
    if create:
        return data_write_dir("runtime:hindsight-snapshot/cache")
    return data_path("runtime:hindsight-snapshot/cache")


def take(base: Path, api: str, bank: str, key: str, root: Path) -> dict:
    base.mkdir(parents=True, exist_ok=True)
    with locked(base):
        repair(base)
        out, manifest = collect(api, bank, key, root)
        check(out, manifest)
        return write_tree(base, out, manifest)


def cmd_take(args) -> int:
    sync = sync_module()
    base = snapshot_base(args.out, create=True)
    try:
        api, bank, key = sync._conn()
        m = take(base, api, bank, key, get_data_root())
    except (Incomplete, urllib.error.URLError, OSError, ValueError, RuntimeError,
            subprocess.CalledProcessError) as exc:
        print(f"snapshot: {type(exc).__name__}: {exc}; the last good snapshot is untouched", file=sys.stderr)
        return 1
    size = sum(p.stat().st_size for p in (base / CURRENT).rglob("*") if p.is_file())
    print(f"snapshot of bank {m['bank']} at {m['taken_at']}: {base / CURRENT}")
    print("  " + ", ".join(f"{k} {v}" for k, v in m["counts"].items()))
    print(f"  documents listed {m['documents_listed']}, mirrored from files {m['documents_mirrored_from_files']}")
    print(f"  files {m['files']}: unchanged {m['unchanged_files']}, new or changed {m['new_or_changed_files']}, "
          f"removed {m['removed_files']}; {size} bytes")
    return 0


# ============================================================
# status, query, lookup
# ============================================================


def manifest_of(base: Path) -> dict | None:
    try:
        return json.loads((base / CURRENT / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def cmd_status(args) -> int:
    m = manifest_of(snapshot_base(args.out))
    if m is None:
        print("no snapshot")
        return 1
    print(json.dumps(m, ensure_ascii=False, indent=1))
    return 0


# ============================================================
# restore-check: an archived snapshot, taken back and counted (HOS-205)
# ============================================================


def _extract(archive: Path, dest: Path) -> None:
    """Unpack a tar archive (plain, .gz, .bz2, .xz, or .zst) into `dest`. The `data` filter
    refuses absolute paths, `..`, links leaving `dest` and device files."""
    if archive.suffix == ".zst":
        if not shutil.which("zstd"):
            raise Incomplete("zstd is not installed, so a .zst archive cannot be read")
        proc = subprocess.Popen(["zstd", "-dc", "--", str(archive)], stdout=subprocess.PIPE)
        try:
            with tarfile.open(fileobj=proc.stdout, mode="r|") as tf:
                tf.extractall(dest, filter="data")
        finally:
            proc.stdout.close()
            rc = proc.wait()
        if rc != 0:
            raise Incomplete(f"zstd -dc exited {rc}")
        return
    with tarfile.open(archive, mode="r:*") as tf:
        tf.extractall(dest, filter="data")


def _snapshot_root(tree: Path) -> Path:
    """The directory holding `manifest.json`: the tree itself, or one directory below it
    (`current/` when the archive was made from the snapshot directory)."""
    if (tree / MANIFEST).is_file():
        return tree
    found = [p.parent for p in tree.glob(f"*/{MANIFEST}")]
    if len(found) != 1:
        raise Incomplete(f"{len(found)} {MANIFEST} files at the top of the archive, expected 1")
    return found[0]


def restore_check(archive: Path, scratch: Path) -> dict:
    """Unpack `archive` into a fresh directory under `scratch` and compare it with the manifest
    it carries: file count, total bytes, and the per-kind counts `check()` applies to a take.
    Returns the verdict; the unpacked copy is removed either way."""
    work = Path(tempfile.mkdtemp(prefix="restore-check-", dir=scratch))
    try:
        _extract(archive, work)
        root = _snapshot_root(work)
        manifest = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
        out = {p.relative_to(root).as_posix(): p.read_bytes()
               for p in root.rglob("*") if p.is_file() and p.name != MANIFEST}
        got_bytes = sum(len(b) for b in out.values())
        problems = []
        if len(out) != manifest.get("files"):
            problems.append(f"files: {len(out)} restored, manifest says {manifest.get('files')}")
        if "bytes" not in manifest:
            problems.append("bytes: the manifest carries no byte total (taken before HOS-205), not checked")
        elif got_bytes != manifest["bytes"]:
            problems.append(f"bytes: {got_bytes} restored, manifest says {manifest['bytes']}")
        if "sha256" not in manifest:
            problems.append("sha256: the manifest carries no content digest (taken before HOS-205), not checked")
        elif tree_digest(out) != manifest["sha256"]:
            problems.append("sha256: the restored content differs from what was taken")
        try:
            check(out, manifest)
        except Incomplete as exc:
            problems.append(str(exc))
        return {"archive": str(archive), "taken_at": manifest.get("taken_at"), "files": len(out),
                "bytes": got_bytes, "ok": not problems, "problems": problems}
    finally:
        shutil.rmtree(work)


def cmd_restore_check(args) -> int:
    scratch = Path(args.scratch).expanduser().resolve() if args.scratch else data_write_dir("runtime:scratch/tmp")
    try:
        v = restore_check(Path(args.archive).expanduser().resolve(), scratch)
    except (Incomplete, OSError, ValueError, tarfile.TarError) as exc:
        print(f"restore-check: FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"restore-check {'OK' if v['ok'] else 'FAILED'}: {v['archive']}")
    print(f"  snapshot taken {v['taken_at']}; restored {v['files']} files, {v['bytes']} bytes")
    for p in v["problems"]:
        print(f"  {p}")
    return 0 if v["ok"] else 1


def index_query(text: str, top_k: int, timeout: float) -> dict:
    """The local memory index over the snapshot collection, as its own JSON says it."""
    proc = subprocess.run(
        [str(VENV_PYTHON), str(MEMORY_INDEX), "query", "--json", "--collection", COLLECTION,
         "--top-k", str(top_k), "--", text],
        capture_output=True, text=True, timeout=timeout, cwd=str(ROOT), check=False)
    try:
        result = json.loads(proc.stdout or "{}")
    except ValueError:
        result = {}
    if proc.returncode != 0 and not result.get("embed_unavailable"):
        result["error"] = f"memory-index exited {proc.returncode}: {(proc.stderr or '').strip()[:200]}"
    return result


def snapshot_answer(text: str, top_k: int = 6, timeout: float = 20.0, base: Path | None = None) -> dict:
    """What the snapshot holds on `text`, each hit with its Hindsight kind and id, and the snapshot's date."""
    base = base or snapshot_base(None)
    m = manifest_of(base)
    if m is None:
        return {"source": "hindsight-snapshot", "snapshot": None, "hits": [],
                "note": "no snapshot on disk; nothing to fall back to"}
    try:
        result = index_query(text, top_k, timeout)
    except subprocess.TimeoutExpired:
        result = {"error": f"the local index did not answer in {timeout}s"}
    hits = []
    root = get_data_root()
    for h in result.get("hits") or []:
        meta = {}
        with contextlib.suppress(OSError, ValueError):
            meta = read_item(root / str(h.get("path")))[0]
        hits.append({"kind": meta.get("hindsight_kind"), "id": meta.get("hindsight_id"),
                     "title": h.get("title") or meta.get("title"), "path": h.get("path"),
                     "score": h.get("score")})
    out = {"source": "hindsight-snapshot", "snapshot": m["taken_at"][:10], "taken_at": m["taken_at"],
           "hits": hits, "near_miss": bool(result.get("near_miss")), "gap": not hits}
    for k in ("embed_unavailable", "error"):
        if result.get(k):
            out[k] = result[k]
    return out


def recall_hindsight(text: str, top_k: int, timeout: float) -> list[dict]:
    """Hindsight's own recall; raises on anything that is not an answer."""
    sync = sync_module()
    api, bank, key = sync._conn()
    if not api.startswith(("http://", "https://")):
        raise ValueError(f"not an http(s) URL: {api}")
    req = urllib.request.Request(  # noqa: S310 - scheme checked above
        f"{api}/v1/default/banks/{bank}/memories/recall", method="POST",
        data=json.dumps({"query": text[:800], "budget": "low", "max_tokens": 1500}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}",
                 "User-Agent": user_agent()})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        results = json.load(r).get("results")
    if not isinstance(results, list):
        raise ValueError("the recall answer carried no results")
    return [{"text": " ".join(str(x.get("text") or "").split()), "document_id": x.get("document_id"),
             "type": x.get("fact_type")} for x in results[:top_k]]


def lookup(text: str, top_k: int = 6, timeout: float = 15.0, base: Path | None = None) -> dict:
    try:
        return {"source": "hindsight", "hits": recall_hindsight(text, top_k, timeout)}
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        why = f"{type(exc).__name__}: {exc}"
    out = snapshot_answer(text, top_k, base=base)
    out["hindsight_unavailable"] = why
    return out


def print_answer(ans: dict) -> None:
    if ans["source"] == "hindsight":
        for h in ans["hits"]:
            print(f"- {h['text'][:300]} -- source: {h['document_id'] or h['type']}")
        if not ans["hits"]:
            print("Hindsight: nothing recalled.")
        return
    if ans.get("hindsight_unavailable"):
        print(f"Hindsight did not answer ({ans['hindsight_unavailable']}).")
    if not ans.get("snapshot"):
        print(ans.get("note", "no snapshot"))
        return
    print(f"From snapshot of {ans['snapshot']} (taken {ans['taken_at']}):")
    for k in ("embed_unavailable", "error"):
        if ans.get(k):
            print(f"  the local index did not run: {ans[k]}")
    for h in ans["hits"]:
        print(f"- [{h['kind']}] {h['title']} -- {h['id']} -- `{h['path']}`")
    if ans["gap"] and not ans.get("error") and not ans.get("embed_unavailable"):
        print("  nothing in the snapshot matched")


def cmd_query(args) -> int:
    ans = snapshot_answer(args.text, args.top_k, args.timeout)
    print(json.dumps(ans, ensure_ascii=False)) if args.json else print_answer(ans)
    return 0 if ans.get("snapshot") else 1


def cmd_lookup(args) -> int:
    ans = lookup(args.text, args.top_k)
    print(json.dumps(ans, ensure_ascii=False)) if args.json else print_answer(ans)
    return 0


# ============================================================
# Self-test: an invented bank on a loopback port; no real server, no real data
# ============================================================


class _Bank:
    """An invented bank. `fail_after` makes the server answer 500 after that many requests;
    `grow` makes the observation total move between pages."""

    def __init__(self, n_obs: int = 5):
        self.docs = {
            "notes/kept.md": {"text": "a mirrored file's text", "tags": ["workspace", "area:notes/kept.md"]},
            "notes/gone.md": {"text": "a file that left the repository", "tags": ["workspace"]},
            "conversation:0000-invented": {"text": "line\n" * 4000, "tags": ["source:chat"]},
            "memory:invented-fact": {"text": "an invented remembered fact", "tags": ["memory"]},
        }
        self.obs = [{"id": f"obs-{i:03d}", "text": f"invented observation {i}", "tags": [],
                     "mentioned_at": "2026-01-02T00:00:00Z", "source_memory_ids": ["a", "b"]} for i in range(n_obs)]
        self.models = [{"id": "mm-1", "name": "Invented page", "source_query": "q", "content": "page body",
                        "tags": ["knowledge:concept"], "created_at": "2026-01-01"}]
        self.directives = [{"id": "dir-1", "name": "Invented rule", "content": "be invented", "priority": 1,
                            "is_active": False}]
        self.bundle = [{"path": "index.md", "content": "# index"}, {"path": "kp-1.md", "content": "page"},
                       {"path": "kp-1.log.md", "content": "log"}]
        self.fail_after = None
        self.grow = False
        self.requests = 0
        self.recall_delay = 0.0   # seconds the recall answer waits, then 500: a slow server that fails

    def answer(self, method: str, path: str, query: dict, body: object = None) -> tuple[int, dict]:
        self.requests += 1
        if self.fail_after is not None and self.requests > self.fail_after:
            return 500, {"detail": "invented failure"}
        lim, off = int(query.get("limit", ["100"])[0]), int(query.get("offset", ["0"])[0])
        base = "/v1/default/banks/inv/"
        if not path.startswith(base):
            return 404, {}
        rest = path[len(base):]
        if rest == "documents":
            items = [{"id": i, "tags": d["tags"]} for i, d in sorted(self.docs.items())]
            return 200, {"items": items[off:off + lim], "total": len(items)}
        if rest.startswith("documents/"):
            i = urllib.parse.unquote(rest[len("documents/"):])
            d = self.docs.get(i)
            return (404, {}) if d is None else (200, {"id": i, "original_text": d["text"], "tags": d["tags"],
                                                      "created_at": "2026-01-01T00:00:00Z"})
        if rest == "memories/list":
            total = len(self.obs) + (self.requests if self.grow else 0)
            return 200, {"items": self.obs[off:off + lim], "total": total}
        if rest == "mental-models":
            return 200, {"items": self.models[off:off + lim], "total": len(self.models)}
        if rest == "directives":
            return 200, {"items": self.directives[off:off + lim], "total": len(self.directives)}
        if rest == "knowledge-base/export":
            return 200, {"files": self.bundle}
        if rest == "memories/recall" and method == "POST":
            if self.recall_delay:
                time.sleep(self.recall_delay)
                return 500, {"detail": "invented slow failure"}
            return 200, {"results": [{"text": "an invented recalled fact", "document_id": "memory:invented-fact"}]}
        return 404, {}


def _serve(bank: _Bank):
    return serve_loopback(bank.answer)


def _closed_port() -> str:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


@contextlib.contextmanager
def _fixture_sync(tmp: Path, api: str):
    """Point the sync module's inputs at an invented data tree: one tracked file, an empty ledger."""
    sync = sync_module()
    root = tmp / "data"
    (root / "notes").mkdir(parents=True, exist_ok=True)
    (root / "notes" / "kept.md").write_text("a mirrored file's text\n", encoding="utf-8")
    saved = {n: getattr(sync, n) for n in ("_cleanup_inputs", "_conn")}
    sync._cleanup_inputs = lambda r: ({"notes/kept.md"}, {}, ())
    sync._conn = lambda: (api, "inv", "invented-key")
    try:
        yield root
    finally:
        for n, v in saved.items():
            setattr(sync, n, v)


def _st_takes_only_what_lives_only_in_hindsight(tmp: Path) -> None:
    bank = _Bank()
    with _serve(bank) as api, _fixture_sync(tmp, api) as root:
        m = take(tmp / "snap", api, "inv", "k", root)
    c = m["counts"]
    assert c == {"observations": 5, "documents": 3, "mental-models": 1, "directives": 1,
                 "knowledge-base-files": 3}, c
    assert m["documents_mirrored_from_files"] == 1
    cur = tmp / "snap" / CURRENT
    ids = {read_item(p)[0]["hindsight_id"] for p in (cur / "documents").glob("*.md")}
    assert ids == {"notes/gone.md", "conversation:0000-invented", "memory:invented-fact"}, ids
    assert (cur / "knowledge-base" / "logs" / "kp-1.log.md").is_file()
    parts = sorted((cur / "documents").glob(stem("conversation:0000-invented") + ".p*.md"))
    assert len(parts) > 1, "a long document is cut into parts"
    assert "".join(read_item(p)[1] for p in parts) == bank.docs["conversation:0000-invented"]["text"]


def _st_a_failed_export_leaves_the_last_good_snapshot(tmp: Path) -> None:
    bank = _Bank()
    with _serve(bank) as api, _fixture_sync(tmp, api) as root:
        take(tmp / "snap", api, "inv", "k", root)
        before = {p: p.read_bytes() for p in (tmp / "snap" / CURRENT).rglob("*") if p.is_file()}
        bank.obs.append({"id": "obs-new", "text": "x"})
        for setup in (lambda: setattr(bank, "fail_after", bank.requests + 3),
                      lambda: (setattr(bank, "fail_after", None), setattr(bank, "grow", True))):
            setup()
            try:
                take(tmp / "snap", api, "inv", "k", root)
            except (Incomplete, urllib.error.URLError):
                pass
            else:
                raise AssertionError("an incomplete export was accepted")
            after = {p: p.read_bytes() for p in (tmp / "snap" / CURRENT).rglob("*") if p.is_file()}
            assert after == before, "the last good snapshot changed"
            assert not list((tmp / "snap").glob("next-*")), "a half-built snapshot was left behind"


def _st_unchanged_files_keep_their_mtime_and_a_crash_is_repaired(tmp: Path) -> None:
    bank = _Bank()
    with _serve(bank) as api, _fixture_sync(tmp, api) as root:
        take(tmp / "snap", api, "inv", "k", root)
        f = tmp / "snap" / CURRENT / "observations" / f"{stem('obs-001')}.md"
        os.utime(f, (1_000_000_000, 1_000_000_000))
        bank.obs[0]["text"] = "changed"
        m = take(tmp / "snap", api, "inv", "k", root)
        assert f.stat().st_mtime == 1_000_000_000, "an unchanged file was rewritten"
        assert m["new_or_changed_files"] == 1 and m["removed_files"] == 0, m
        os.rename(tmp / "snap" / CURRENT, tmp / "snap" / ".old-1")
        (tmp / "snap" / "next-1").mkdir()
        take(tmp / "snap", api, "inv", "k", root)
        assert (tmp / "snap" / CURRENT / MANIFEST).is_file()
        assert not list((tmp / "snap").glob(".old-*")) and not list((tmp / "snap").glob("next-*"))


def _st_lookup_falls_back_to_the_snapshot_when_hindsight_does_not_answer(tmp: Path) -> None:
    global index_query
    bank = _Bank()
    with _serve(bank) as api, _fixture_sync(tmp, api) as root:
        take(tmp / "snap", api, "inv", "k", root)
    hit = next((tmp / "snap" / CURRENT / "observations").glob("*.md"))
    real_q, real_root = index_query, globals()["get_data_root"]
    index_query = lambda text, top_k, timeout: {"hits": [{"path": hit.relative_to(tmp).as_posix(),  # noqa: E731
                                                          "title": "t", "score": 0.7}]}
    globals()["get_data_root"] = lambda: tmp
    try:
        with _serve(bank) as api, _fixture_sync(tmp, api):
            up = lookup("an invented question", base=tmp / "snap")
        assert up["source"] == "hindsight" and up["hits"], up
        with _fixture_sync(tmp, _closed_port()):
            down = lookup("an invented question", base=tmp / "snap", timeout=3)
        assert down["source"] == "hindsight-snapshot" and "hindsight_unavailable" in down, down
        assert down["snapshot"] == manifest_of(tmp / "snap")["taken_at"][:10]
        assert down["hits"][0]["kind"] == "observation" and down["hits"][0]["id"].startswith("obs-"), down
        bank.fail_after = 0   # the server answers, with an error: also not an answer
        with _serve(bank) as api, _fixture_sync(tmp, api):
            assert lookup("an invented question", base=tmp / "snap")["source"] == "hindsight-snapshot"
        assert snapshot_answer("q", base=tmp / "none")["snapshot"] is None
    finally:
        index_query, globals()["get_data_root"] = real_q, real_root


def _st_the_snapshot_never_loops_back_into_the_bank(tmp: Path) -> None:
    sync = sync_module()
    store = data_path("runtime:hindsight-snapshot/cache", data_root=tmp)
    rel = (store / CURRENT / "observations" / "x.md").relative_to(tmp).as_posix()
    assert sync.excluded_why(rel, set()) is not None, "hindsight-sync would send the snapshot"


def _st_the_index_layer_reaches_the_snapshot_and_nothing_else_under_the_store(tmp: Path) -> None:
    spec = importlib.util.spec_from_file_location("memory_index_for_snapshot", MEMORY_INDEX)
    mi = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mi)
    import yaml
    cfg = yaml.safe_load((ROOT / "config" / "memory-index.yaml").read_text(encoding="utf-8"))
    from scripts.utils.memory_layers import resolve_layer
    layer = resolve_layer(next(lc for lc in cfg["layers"] if lc["layer"] == COLLECTION), tmp)
    assert cfg["collections"][COLLECTION] == [COLLECTION]
    assert COLLECTION not in cfg["collections"]["content"]
    denies = list(cfg["deny_prefixes"])
    store, service = mi.default_store_dirname(), SNAPSHOT_ADDR.split(":", 1)[1]
    assert f"{store}/" in denies and "_secure/" in denies, denies
    allowed = mi.layer_deny_prefixes(layer, denies)
    other = mi.layer_deny_prefixes({"layer": "x", "glob": "a/*.md"}, denies)
    assert f"{store}/" not in allowed and "_secure/" in allowed and other == denies
    rx = [mi.glob_to_regex(g) for g in mi.expand_brace_glob(layer["glob"])]
    for rel, want in ((f"{store}/{service}/{CURRENT}/observations/a.md", True),
                      (f"{store}/{service}/{CURRENT}/documents/a.p001.md", True),
                      (f"{store}/{service}/{CURRENT}/knowledge-base/logs/kp.log.md", False),
                      (f"{store}/{service}/next-1/observations/a.md", False),
                      (f"{store}/an-invented-service/cache/a.md", False)):
        assert any(r.match(rel) for r in rx) is want, rel


HOOK = ROOT / ".claude" / "hooks" / "hindsight-recall-inject.py"
INVENTED_ANSWER = {"source": "hindsight-snapshot", "snapshot": "2026-01-02", "taken_at": "2026-01-02T03:04:05+00:00",
                   "hits": [{"kind": "observation", "id": "obs-001", "title": "an invented observation",
                             "path": "an/invented/observation.md"}]}


def hook_scenario(tmp: Path, hook_source: Path, api: str, snapshot_delay: float = 0.0) -> str:
    """Run a copy of the per-prompt hook in a throwaway workspace whose snapshot command is a
    stub printing `INVENTED_ANSWER` after `snapshot_delay` seconds, against `api`; return what
    the hook printed. Its outcome log goes to `tmp/state/runs.jsonl`."""
    ws = tmp / "ws"
    (ws / ".claude" / "hooks").mkdir(parents=True, exist_ok=True)
    (ws / "scripts").mkdir(exist_ok=True)
    (ws / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(hook_source, ws / ".claude" / "hooks" / "hindsight-recall-inject.py")
    (ws / "scripts" / "hindsight-snapshot.py").write_text(
        f"import json, time\ntime.sleep({snapshot_delay})\nprint(json.dumps({json.dumps(INVENTED_ANSWER)}))\n",
        encoding="utf-8")
    wrapper = ws / ".venv" / "bin" / "python"
    wrapper.write_text(f'#!/bin/sh\nexec "{VENV_PYTHON}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    key = tmp / "invented-key"
    key.write_text("invented-key\n", encoding="utf-8")
    env = {**os.environ, "HINDSIGHT_INJECT_URL": api, "HINDSIGHT_INJECT_BANK": "inv",
           "HINDSIGHT_INJECT_KEY_FILE": str(key), "HINDSIGHT_INJECT_TIMEOUT": "3",
           "HINDSIGHT_INJECT_BUDGET_S": "6", "HINDSIGHT_INJECT_STATE_DIR": str(tmp / "state")}
    prompt = json.dumps({"prompt": "what did we decide about the invented subject last week"})
    proc = subprocess.run(["python3", str(ws / ".claude" / "hooks" / "hindsight-recall-inject.py")],
                          input=prompt, capture_output=True, text=True, env=env, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _st_the_prompt_hook_answers_from_the_snapshot_when_hindsight_is_down(tmp: Path) -> None:
    down = hook_scenario(tmp, HOOK, _closed_port())
    ctx = json.loads(down)["hookSpecificOutput"]["additionalContext"]
    assert 'source="hindsight-snapshot"' in ctx and "snapshot of 2026-01-02" in ctx and "obs-001" in ctx, ctx
    with _serve(_Bank()) as api:
        up = json.loads(hook_scenario(tmp, HOOK, api))["hookSpecificOutput"]["additionalContext"]
    assert 'source="hindsight"' in up and "snapshot" not in up, up
    runs = [json.loads(x) for x in (tmp / "state" / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["outcome"] for r in runs] == ["snapshot", "hindsight"], runs
    assert runs[0]["error"] and runs[1]["hindsight_s"] is not None, runs


def _st_the_prompt_hook_asks_hindsight_and_the_snapshot_at_once(tmp: Path) -> None:
    """Operator decision, 2026-10-06: run both lookups at the same time. A server that fails after 2 s and a
    snapshot that answers after 1.5 s take about 2 s together, and 3.5 s one after the other."""
    bank = _Bank()
    bank.recall_delay = 2.0
    with _serve(bank) as api:
        t0 = time.monotonic()
        out = hook_scenario(tmp, HOOK, api, snapshot_delay=1.5)
        took = time.monotonic() - t0
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert 'source="hindsight-snapshot"' in ctx and "obs-001" in ctx, ctx
    assert took < 3.2, f"the two lookups ran one after the other: {took:.2f}s"
    run = json.loads((tmp / "state" / "runs.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert run["outcome"] == "snapshot" and run["total_s"] < 3.2, run


def _archive(src: Path, dest: Path, drop: str | None = None, patch: str | None = None,
             grow: str | None = None) -> Path:
    """A tar.gz of `src` as `current/`, optionally without one file, with its last byte changed,
    or with one byte appended to it."""
    with tarfile.open(dest, "w:gz" if dest.suffix == ".gz" else "w") as tf:
        for p in sorted(src.rglob("*")):
            rel = p.relative_to(src).as_posix()
            if not p.is_file() or rel == drop:
                continue
            data = p.read_bytes()
            if rel == patch:
                data = data[:-1] + (b"X" if data[-1:] != b"X" else b"Y")
            if rel == grow:
                data += b"\n"
            info = tarfile.TarInfo(f"{CURRENT}/{rel}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return dest


def _st_restore_check_passes_a_whole_archive_and_refuses_a_damaged_one(tmp: Path) -> None:
    bank = _Bank()
    with _serve(bank) as api, _fixture_sync(tmp, api) as root:
        take(tmp / "snap", api, "inv", "k", root)
    cur = tmp / "snap" / CURRENT
    scratch = tmp / "scratch"
    scratch.mkdir()
    good = restore_check(_archive(cur, tmp / "good.tar.gz"), scratch)
    assert good["ok"] and good["files"] == manifest_of(tmp / "snap")["files"] > 0, good
    obs = next((cur / "observations").glob("*.md")).relative_to(cur).as_posix()
    short = restore_check(_archive(cur, tmp / "short.tar.gz", drop=obs), scratch)
    assert not short["ok"] and any(p.startswith("files:") for p in short["problems"]), short
    longer = restore_check(_archive(cur, tmp / "longer.tar.gz", grow=obs), scratch)
    assert not longer["ok"] and any(p.startswith("bytes:") for p in longer["problems"]), longer
    bent = restore_check(_archive(cur, tmp / "bent.tar.gz", patch=obs), scratch)
    assert not bent["ok"] and bent["problems"] == ["sha256: the restored content differs from what was taken"], bent
    assert not list(scratch.iterdir()), "an unpacked copy was left in scratch"
    if shutil.which("zstd"):
        tar = _archive(cur, tmp / "plain.tar")
        subprocess.run(["zstd", "-q", "-19", "-o", str(tmp / "z.tar.zst"), "--", str(tar)], check=True)
        assert restore_check(tmp / "z.tar.zst", scratch)["ok"], "a .zst archive was not read"
    assert main(["restore-check", str(tmp / "good.tar.gz"), "--scratch", str(scratch)]) == 0
    assert main(["restore-check", str(tmp / "short.tar.gz"), "--scratch", str(scratch)]) == 1


def _st_restore_check_refuses_a_member_that_leaves_its_directory(tmp: Path) -> None:
    (tmp / "scratch").mkdir()
    plain = tmp / "evil.tar"
    with tarfile.open(plain, "w") as tf:
        info = tarfile.TarInfo("../escaped.md")
        info.size = 1
        tf.addfile(info, io.BytesIO(b"x"))
    archives = [plain]
    if shutil.which("zstd"):
        subprocess.run(["zstd", "-q", "-o", str(tmp / "evil.tar.zst"), "--", str(plain)], check=True)
        archives.append(tmp / "evil.tar.zst")
    for evil in archives:
        try:
            restore_check(evil, tmp / "scratch")
        except tarfile.TarError:
            pass
        else:
            raise AssertionError(f"{evil.name}: a member outside the restore directory was accepted")
        assert not (tmp / "escaped.md").exists() and not list((tmp / "scratch").iterdir()), evil.name


SELF_TESTS = [
    _st_takes_only_what_lives_only_in_hindsight,
    _st_a_failed_export_leaves_the_last_good_snapshot,
    _st_unchanged_files_keep_their_mtime_and_a_crash_is_repaired,
    _st_lookup_falls_back_to_the_snapshot_when_hindsight_does_not_answer,
    _st_the_snapshot_never_loops_back_into_the_bank,
    _st_the_index_layer_reaches_the_snapshot_and_nothing_else_under_the_store,
    _st_the_prompt_hook_answers_from_the_snapshot_when_hindsight_is_down,
    _st_the_prompt_hook_asks_hindsight_and_the_snapshot_at_once,
    _st_restore_check_passes_a_whole_archive_and_refuses_a_damaged_one,
    _st_restore_check_refuses_a_member_that_leaves_its_directory,
]


def cmd_self_test(_args) -> int:
    failed = 0
    for t in SELF_TESTS:
        with tempfile.TemporaryDirectory(dir=data_write_dir("runtime:scratch/tmp")) as d:
            try:
                t(Path(d))
                print(f"PASS {t.__name__}")
            except Exception as exc:  # noqa: BLE001 - every scenario reports, none stops the rest
                failed += 1
                print(f"FAIL {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"{len(SELF_TESTS) - failed}/{len(SELF_TESTS)} passed")
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[1], formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("take", help="export what lives only in Hindsight; the last good snapshot survives a failure")
    t.add_argument("--out", help="snapshot directory (default: the registry address)")
    t.set_defaults(fn=cmd_take)
    s = sub.add_parser("status", help="the current snapshot's manifest")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_status)
    for name, fn, help_ in (("query", cmd_query, "search the snapshot alone, through the local index"),
                            ("lookup", cmd_lookup, "ask Hindsight; answer from the snapshot when it does not")):
        q = sub.add_parser(name, help=help_)
        q.add_argument("text")
        q.add_argument("--json", action="store_true")
        q.add_argument("--top-k", type=int, default=6)
        if name == "query":
            q.add_argument("--timeout", type=float, default=20.0)
        q.set_defaults(fn=fn)
    r = sub.add_parser("restore-check", help="unpack an archived snapshot into scratch and compare it with its manifest")
    r.add_argument("archive", help="a tar of the snapshot directory or of current/ (.tar, .gz, .bz2, .xz, .zst)")
    r.add_argument("--scratch", help="where to unpack (default: the registry address runtime:scratch/tmp)")
    r.set_defaults(fn=cmd_restore_check)
    sub.add_parser("self-test", help="the scenarios on an invented bank; no real server").set_defaults(fn=cmd_self_test)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
