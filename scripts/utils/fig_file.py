"""Open a Figma .fig file and decode its document tree, in memory.

A .fig is a zip holding `canvas.fig`, `images/`, `meta.json` and
`thumbnail.png`. `canvas.fig` is the magic `fig-kiwi`, a little-endian uint32
version, then chunks, each a little-endian uint32 length and its bytes: chunk 0
is the kiwi schema (raw deflate), chunk 1 the document (zstd). The zstd step
runs the `zstd` program, because the engine carries no zstd library.

The file is untrusted input. Every member is read in memory, never extracted;
only `canvas.fig`, `meta.json` and `images/<40 hex>` are read; every size is
capped by a module constant below; and every failure while reading becomes a
`FigFileError` with a one-line reason, so a caller never shows a traceback for
a bad file.

The decoded tree is cached as `tree.json` under
`<runtime:fig-design/cache>/<sha256>-v<DECODER_VERSION>/`; raise DECODER_VERSION
whenever decoding changes, so an old cache is never read as a new one.

Public interface: `load(path) -> FigFile`, `FigFile.sha256`, `.tree`, `.meta`,
`.image(hash_hex)`, `digest(fig) -> dict`, `FigFileError`.
"""
from __future__ import annotations

import base64
import collections
import hashlib
import json
import re
import shutil
import struct
import subprocess
import sys
import threading
import zipfile
import zlib
from pathlib import Path

from scripts.utils.atomic import atomic_write_text
from scripts.utils.data_path import DataPathError, data_write_dir
from scripts.utils.paths import DataRootError

# ---------------------------------------------------------------------------
# Limits. Each one refuses the file with a FigFileError when crossed.
# ---------------------------------------------------------------------------
MAX_ZIP_MEMBERS = 20000
MAX_MEMBER_BYTES = 512 * 1024 * 1024          # uncompressed, per member read
MAX_DECOMPRESSED_BYTES = 1024 * 1024 * 1024   # each of the deflate and zstd steps
ZSTD_TIMEOUT_SECONDS = 120
ZSTD_READ_BYTES = 1024 * 1024
MAX_NESTING = 64                              # nested kiwi messages and structs
MAX_ARRAY_LENGTH = 50_000_000

MAGIC = b"fig-kiwi"
DECODER_VERSION = 1

_IMAGE_RE = re.compile(r"[0-9a-f]{40}")
_CANVAS = "canvas.fig"
_META = "meta.json"


class FigFileError(Exception):
    """A .fig file that cannot be read, with a one-line reason."""


# ---------------------------------------------------------------------------
# Kiwi decoding (the binary schema format Figma uses)
# ---------------------------------------------------------------------------
class _Reader:
    __slots__ = ("b", "i")

    def __init__(self, b: bytes):
        self.b = b
        self.i = 0

    def byte(self) -> int:
        v = self.b[self.i]
        self.i += 1
        return v

    def vu(self) -> int:
        v = 0
        sh = 0
        while True:
            c = self.byte()
            v |= (c & 127) << sh
            sh += 7
            if c < 128 or sh >= 35:
                return v

    def vu64(self) -> int:
        v = 0
        sh = 0
        while True:
            c = self.byte()
            v |= (c & 127) << sh
            sh += 7
            if c < 128 or sh >= 70:
                return v

    def vi(self) -> int:
        v = self.vu()
        return (v >> 1) ^ -(v & 1)

    def vi64(self) -> int:
        v = self.vu64()
        return (v >> 1) ^ -(v & 1)

    def f(self) -> float:
        c = self.byte()
        if c == 0:
            return 0.0
        b = self.b
        i = self.i
        bits = c | (b[i] << 8) | (b[i + 1] << 16) | (b[i + 2] << 24)
        self.i += 3
        bits = ((bits << 23) | (bits >> 9)) & 0xFFFFFFFF
        return struct.unpack("<f", struct.pack("<I", bits))[0]

    def s(self) -> str:
        j = self.b.index(0, self.i)
        v = self.b[self.i:j].decode("utf-8", "replace")
        self.i = j + 1
        return v

    def count(self) -> int:
        """An array or list length, refused when it cannot be real."""
        n = self.vu()
        if n > MAX_ARRAY_LENGTH or n > len(self.b) - self.i:
            raise ValueError(f"declared length {n} exceeds the bytes left "
                             f"({len(self.b) - self.i}) or the limit {MAX_ARRAY_LENGTH}")
        return n


def _schema(b: bytes) -> list:
    r = _Reader(b)
    defs = []
    for _ in range(r.count()):
        name = r.s()
        kind = r.byte()
        fs = []
        for _ in range(r.count()):
            fn = r.s()
            t = r.vi()
            arr = r.byte()
            val = r.vu()
            fs.append((fn, t, arr, val))
        defs.append((name, kind, fs))
    return defs


def _decode(defs: list, b: bytes, root: str = "Message") -> dict:
    r = _Reader(b)
    idx = {d[0]: i for i, d in enumerate(defs)}
    enums = {i: {f[3]: f[0] for f in d[2]} for i, d in enumerate(defs) if d[1] == 0}
    fields = {i: {f[3]: f for f in d[2]} for i, d in enumerate(defs) if d[1] not in (0, 1)}
    prim = {-1: lambda: bool(r.byte()), -2: r.byte, -3: r.vi, -4: r.vu,
            -5: r.f, -6: r.s, -7: r.vi64, -8: r.vu64}

    def val(t, depth):
        if t < 0:
            return prim[t]()
        return rd(t, depth)

    def fld(t, arr, depth):
        if arr:
            n = r.count()
            if t == -2:
                v = bytes(r.b[r.i:r.i + n])
                r.i += n
                return base64.b64encode(v).decode()
            return [val(t, depth) for _ in range(n)]
        return val(t, depth)

    def rd(ti, depth):
        if depth > MAX_NESTING:
            raise ValueError(f"messages nested deeper than {MAX_NESTING}")
        if ti < 0 or ti >= len(defs):
            raise IndexError(f"type index {ti} is not in the schema")
        _name, kind, fs = defs[ti]
        if kind == 0:
            v = r.vu()
            return enums[ti].get(v, v)
        if kind == 1:
            return {fn: fld(t, a, depth + 1) for fn, t, a, _ in fs}
        out = {}
        m = fields[ti]
        while True:
            k = r.vu()
            if k == 0:
                return out
            fn, t, a, _ = m[k]
            out[fn] = fld(t, a, depth + 1)

    return rd(idx[root], 1)


# ---------------------------------------------------------------------------
# Container steps
# ---------------------------------------------------------------------------
def _chunks(canvas: bytes) -> list[bytes]:
    if canvas[:8] != MAGIC:
        raise FigFileError(f"canvas.fig does not start with {MAGIC.decode()!r}")
    if len(canvas) < 12:
        raise FigFileError("canvas.fig is truncated before its version")
    i = 12
    out = []
    while i < len(canvas):
        if i + 4 > len(canvas):
            raise FigFileError(f"canvas.fig is truncated in a chunk length at byte {i}")
        (n,) = struct.unpack_from("<I", canvas, i)
        i += 4
        if n > len(canvas) - i:
            raise FigFileError(f"canvas.fig chunk {len(out)} declares {n} bytes, "
                               f"{len(canvas) - i} are left")
        out.append(canvas[i:i + n])
        i += n
    if len(out) < 2:
        raise FigFileError(f"canvas.fig has {len(out)} chunk(s), at least 2 are needed")
    return out


def _inflate_raw(data: bytes) -> bytes:
    d = zlib.decompressobj(-15)
    out = d.decompress(data, MAX_DECOMPRESSED_BYTES + 1)
    if len(out) > MAX_DECOMPRESSED_BYTES or d.unconsumed_tail:
        raise FigFileError(f"the schema chunk inflates past {MAX_DECOMPRESSED_BYTES} bytes")
    return out


def _unzstd(data: bytes) -> bytes:
    """Decompress with the `zstd` program, streaming and capped.

    stdin is fed from a thread and stderr drained by another, so neither pipe
    can fill and stall the process; stdout is read in ZSTD_READ_BYTES reads and
    the process is killed the moment the total passes MAX_DECOMPRESSED_BYTES,
    or when ZSTD_TIMEOUT_SECONDS have passed.
    """
    exe = shutil.which("zstd")
    if not exe:
        raise FigFileError("the zstd program is not installed; it decompresses the "
                           "document chunk (install the zstd package)")
    proc = subprocess.Popen([exe, "-d", "-c"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    state = {"timed_out": False, "killed": False, "write_error": None}
    err_parts: list[bytes] = []

    def feed():
        try:
            proc.stdin.write(data)
            proc.stdin.close()
        except OSError as exc:  # a killed process closes the pipe: recorded, judged below
            state["write_error"] = exc

    def drain():
        err_parts.append(proc.stderr.read(64 * 1024))
        proc.stderr.read()

    def on_timeout():
        state["timed_out"] = True
        proc.kill()

    timer = threading.Timer(ZSTD_TIMEOUT_SECONDS, on_timeout)
    feeder = threading.Thread(target=feed, daemon=True)
    drainer = threading.Thread(target=drain, daemon=True)
    timer.start()
    feeder.start()
    drainer.start()
    parts = []
    total = 0
    try:
        while True:
            buf = proc.stdout.read(ZSTD_READ_BYTES)
            if not buf:
                break
            total += len(buf)
            if total > MAX_DECOMPRESSED_BYTES:
                state["killed"] = True
                proc.kill()
                break
            parts.append(buf)
    finally:
        rc = proc.wait()
        timer.cancel()
        feeder.join(5)
        drainer.join(5)
        proc.stdout.close()
    if state["timed_out"]:
        raise FigFileError(f"zstd did not finish within {ZSTD_TIMEOUT_SECONDS} s")
    if state["killed"]:
        raise FigFileError(f"the document chunk decompresses past {MAX_DECOMPRESSED_BYTES} bytes")
    if rc != 0:
        msg = (b"".join(err_parts).decode("utf-8", "replace").strip().splitlines() or [""])[-1]
        raise FigFileError(f"zstd exited {rc} on the document chunk: {msg}")
    if state["write_error"] is not None:
        raise FigFileError(f"could not feed zstd: {state['write_error']}")
    return b"".join(parts)


def _read_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    if info.file_size > MAX_MEMBER_BYTES:
        raise FigFileError(f"zip member {info.filename} declares {info.file_size} bytes, "
                           f"the limit is {MAX_MEMBER_BYTES}")
    with zf.open(info) as fh:
        data = fh.read(MAX_MEMBER_BYTES + 1)
    if len(data) > MAX_MEMBER_BYTES:
        raise FigFileError(f"zip member {info.filename} is larger than {MAX_MEMBER_BYTES} bytes")
    return data


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _one_line(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


# ---------------------------------------------------------------------------
# FigFile
# ---------------------------------------------------------------------------
class FigFile:
    """An open .fig: its hash, decoded tree, meta.json and image lookup."""

    def __init__(self, path: Path, sha256: str, zf: zipfile.ZipFile, members: dict,
                 tree: dict, meta: dict):
        self.path = path
        self.sha256 = sha256
        self.tree = tree
        self.meta = meta
        self._zf = zf
        self._members = members

    def image(self, hash_hex: str) -> bytes | None:
        """The bytes of `images/<hash_hex>`, or None when there is no such member."""
        if not isinstance(hash_hex, str) or not _IMAGE_RE.fullmatch(hash_hex):
            return None
        info = self._members.get("images/" + hash_hex)
        if info is None:
            return None
        try:
            return _read_member(self._zf, info)
        # RuntimeError: an encrypted member; NotImplementedError: an unsupported compression method.
        except (zipfile.BadZipFile, zlib.error, OSError, EOFError, ValueError, RuntimeError,
                NotImplementedError) as exc:
            raise FigFileError(f"image {hash_hex}: {_one_line(exc)}") from exc

    def close(self) -> None:
        self._zf.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _decode_canvas(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> dict:
    canvas = _read_member(zf, info)
    chunks = _chunks(canvas)
    schema = _schema(_inflate_raw(chunks[0]))
    return _decode(schema, _unzstd(chunks[1]))


def _cached_tree(cache_file: Path) -> dict | None:
    if not cache_file.is_file():
        return None
    try:
        return json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"fig-design: cache {cache_file} unreadable ({_one_line(exc)}); decoding again",
              file=sys.stderr)
        return None


def load(path) -> FigFile:
    """Open the .fig at `path`, decoding its tree or reading it from the cache."""
    path = Path(path)
    try:
        sha = _sha256(path)
        zf = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile, ValueError, EOFError, NotImplementedError) as exc:
        raise FigFileError(f"{path.name}: not a readable zip ({_one_line(exc)})") from exc
    try:
        infos = zf.infolist()
        if len(infos) > MAX_ZIP_MEMBERS:
            raise FigFileError(f"{path.name}: {len(infos)} zip members, the limit is {MAX_ZIP_MEMBERS}")
        members = {}
        for info in infos:
            name = info.filename
            if name in (_CANVAS, _META) or (name.startswith("images/")
                                            and _IMAGE_RE.fullmatch(name[7:])):
                members[name] = info
        if _CANVAS not in members:
            raise FigFileError(f"{path.name}: the zip holds no canvas.fig")
        meta = {}
        if _META in members:
            meta = json.loads(_read_member(zf, members[_META]).decode("utf-8"))
            if not isinstance(meta, dict):
                raise FigFileError(f"{path.name}: meta.json is not a JSON object")
        cache_dir = data_write_dir("runtime:fig-design/cache") / f"{sha}-v{DECODER_VERSION}"
        cache_file = cache_dir / "tree.json"
        tree = _cached_tree(cache_file)
        if tree is None:
            tree = _decode_canvas(zf, members[_CANVAS])
            cache_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_text(cache_file, json.dumps(tree, default=str))
        if not isinstance(tree, dict) or not isinstance(tree.get("nodeChanges", []), list):
            raise FigFileError(f"{path.name}: the decoded document has no nodeChanges list")
    except (FigFileError, DataPathError, DataRootError):
        # DataPathError and DataRootError are RuntimeErrors about the cache address, not the file:
        # it passes through unchanged rather than being reported as a bad .fig.
        zf.close()
        raise
    # RuntimeError: an encrypted canvas.fig or meta.json member (zipfile asks for a password);
    # NotImplementedError: an unsupported compression method.
    except (zipfile.BadZipFile, zlib.error, struct.error, KeyError, IndexError,
            ValueError, EOFError, RecursionError, UnicodeDecodeError, NotImplementedError,
            RuntimeError, OSError) as exc:
        zf.close()
        raise FigFileError(f"{path.name}: {_one_line(exc)}") from exc
    return FigFile(path, sha, zf, members, tree, meta)


# ---------------------------------------------------------------------------
# Digest: tokens, styles, fonts per page, components
# ---------------------------------------------------------------------------
def K(g: dict) -> tuple:
    """A node key from a GUID dict."""
    return (g["sessionID"], g["localID"])


def _hexc(c: dict) -> str:
    return ("#%02x%02x%02x" % tuple(round(c[k] * 255) for k in "rgb")
            + ("" if c.get("a", 1) >= 0.999 else "%02x" % round(c["a"] * 255)))


def digest(fig: FigFile) -> dict:
    """Design-system digest computed from the tree, in memory."""
    ns = fig.tree.get("nodeChanges", [])
    by = {K(n["guid"]): n for n in ns if "guid" in n}

    def par(n):
        return by.get(K(n["parentIndex"]["guid"])) if n.get("parentIndex") else None

    def page(n):
        hops = 0
        while n and n.get("type") != "CANVAS" and hops <= len(by):
            n = par(n)
            hops += 1
        return n.get("name") if n and n.get("type") == "CANVAS" else None

    sets = {K(n["guid"]): n for n in ns if n.get("type") == "VARIABLE_SET"}
    vars_ = {K(n["guid"]): n for n in ns if n.get("type") == "VARIABLE"}
    modes = {}
    for s in sets.values():
        for md in s.get("variableSetModes", []):
            modes[K(md["id"])] = md["name"]

    def val(v, depth=0):
        d = v.get("value", {})
        if "colorValue" in d:
            return _hexc(d["colorValue"])
        if "floatValue" in d:
            return d["floatValue"]
        if "alias" in d and depth < 8:
            t = vars_.get(K(d["alias"]["guid"]))
            if t:
                e = t["variableDataValues"]["entries"][0]["variableData"]
                return {"alias": t["name"], "value": val(e, depth + 1)}
        return d

    toks = []
    for v in vars_.values():
        web = [e["value"] for e in v.get("codeSyntax", {}).get("entries", []) if e["platform"] == "WEB"]
        toks.append({
            "set": sets.get(K(v["variableSetID"]["guid"]), {}).get("name"),
            "name": v["name"],
            "web": web[0] if web else None,
            "type": v.get("variableResolvedType"),
            "values": {modes.get(K(e["modeID"]), str(K(e["modeID"]))): val(e["variableData"])
                       for e in v["variableDataValues"]["entries"]},
        })
    styles = []
    for n in ns:
        if n.get("styleType") == "TEXT":
            styles.append({"name": n["name"], "font": n["fontName"]["family"] + " " + n["fontName"]["style"],
                           "size": n.get("fontSize"), "lineHeight": n.get("lineHeight"),
                           "letterSpacing": n.get("letterSpacing"), "page": page(n)})
        elif n.get("styleType") in ("EFFECT", "FILL", "GRID"):
            styles.append({"name": n["name"], "styleType": n["styleType"],
                           "fills": [_hexc(p["color"]) for p in n.get("fillPaints", []) if p.get("color")],
                           "effects": n.get("effects")})
    fp = collections.defaultdict(collections.Counter)
    for n in ns:
        if n.get("fontName"):
            fp[page(n)][n["fontName"]["family"] + " " + n["fontName"]["style"]] += 1
    comps = collections.defaultdict(list)
    for n in ns:
        if n.get("type") == "SYMBOL":
            p = par(n)
            grp = (p.get("name") if p and p.get("type") == "FRAME" and p.get("isStateGroup")
                   else (p.get("name") if p else ""))
            comps[page(n)].append({"name": n["name"], "set": grp, "w": round(n["size"]["x"]),
                                   "h": round(n["size"]["y"]), "desc": n.get("symbolDescription")})
    return {"tokens": toks, "styles": styles,
            "fonts_per_page": {k: dict(v) for k, v in fp.items()}, "components": dict(comps)}
