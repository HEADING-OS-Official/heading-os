"""The local semantic index for `scripts/heading-install.py`: ollama, bge-m3, the first build (HOS-305).

Slice 5 of the deployment design (registry name `plans`,
2026-11-01-heading-os-ideal-deployment-design.md, section 2 step 6): the
embedding server and the `bge-m3` model installed in user space, the first
index build, and the hourly refresh timer, on one machine with no other host.

What it reproduces. `config/memory-index.yaml` names `model: bge-m3` and no
host; with no `config/inference-hosts.yaml` the embedder resolves to
http://localhost:11434 unprobed (`resolve` in `scripts/utils/inference_host.py`),
so the install writes no host file and ollama binds 127.0.0.1:11434.

Ollama comes from the upstream release archive, pinned below by version and
sha256 and checked before anything is unpacked, into
~/.local/share/heading-os/ollama/<version>/, the way `install_node` installs
Node.js. No symlink is created; the unit's ExecStart names the versioned binary
(`{{OLLAMA_BIN}}`, valued by `_extra_value` in `scripts/utils/schedule.py`).
The archive's own library links inside lib/ollama are upstream's. The Linux
asset is `.tar.zst`, which Python 3.11 cannot decompress, so it goes through
the system `zstd` into `tarfile` stream mode, as `scripts/hindsight-snapshot.py`
reads its archives (HELM, HOS-305, 2026-11-01). macOS: the upstream
`ollama-darwin.tgz` by the same shape, UNTESTED on a Mac.

Standard library only at import time: the installer imports this before the
engine's environment exists. Anything that needs the engine runs through
`ctx.python`.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

# Upstream release v0.40.13, published 2026-10-28:
# https://github.com/ollama/ollama/releases/tag/v0.40.13
# Digests read 2026-11-01 from the release's sha256sum.txt; the GitHub release
# API's per-asset `digest` field carries the same three values. They are public
# checksums, not credentials: the pragma on each line tells detect-secrets so.
OLLAMA_VERSION = "0.40.13"
OLLAMA_RELEASE = f"https://github.com/ollama/ollama/releases/download/v{OLLAMA_VERSION}/"
OLLAMA_SHA256 = {
    ("linux", "amd64"): ("ollama-linux-amd64.tar.zst",
                         "c9490e0f520be21271d969e6703f012268b1d39f8b70dd58854e5bfef7a75bb6"),  # pragma: allowlist secret
    ("linux", "arm64"): ("ollama-linux-arm64.tar.zst",
                         "913d9a28da6da86d551ce4ca531457767bb14d511cbc1d5ca93e9b3b8b9a8666"),  # pragma: allowlist secret
    # One universal archive serves both Mac processors.
    ("darwin", "amd64"): ("ollama-darwin.tgz",
                          "bae29252232b9e1358cf5801a7d65a07442af18fb5467cb68c7fc3711f59c511"),  # pragma: allowlist secret
    ("darwin", "arm64"): ("ollama-darwin.tgz",
                          "bae29252232b9e1358cf5801a7d65a07442af18fb5467cb68c7fc3711f59c511"),  # pragma: allowlist secret
}
OLLAMA_ADDRESS = "127.0.0.1:11434"
OLLAMA_URL = f"http://{OLLAMA_ADDRESS}"
EMBED_MODEL = "bge-m3"
SERVER_FAMILY = "embedding-server"
REFRESH_FAMILY = "memory-index-refresh"
CODE_STORE_REL = ".memory-index-code/index.db"   # memory-index.py CODE_STORE_REL
_CHUNK = 1024 * 1024


class InstallError(RuntimeError):
    """A step could not finish; the message is the plain line the user sees."""


# ============================================================
# Where things live
# ============================================================

def ollama_home() -> Path:
    return Path.home() / ".local" / "share" / "heading-os" / "ollama"


def _system() -> str:
    return {"Linux": "linux", "Darwin": "darwin"}.get(platform.system(), platform.system().lower())


def _arch() -> str | None:
    machine = platform.machine().lower()
    return {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)


def ollama_binary(version: str = OLLAMA_VERSION) -> Path:
    """The versioned binary the unit runs: bin/ollama on Linux, ollama at the top on macOS."""
    root = ollama_home() / version
    return root / "ollama" if _system() == "darwin" else root / "bin" / "ollama"


def _client_env(ctx) -> dict[str, str]:
    """The environment for `ollama <command>` as a client of this install's server."""
    env = ctx.env()
    env["OLLAMA_HOST"] = OLLAMA_ADDRESS
    return env


# ============================================================
# Reading the real state
# ============================================================

def api_get(path: str, timeout: int = 5) -> dict | None:
    """GET OLLAMA_URL + path as JSON, or None when nothing answers with a JSON object."""
    from scripts.utils.user_agent import user_agent  # stdlib only
    req = urllib.request.Request(OLLAMA_URL + path, headers={"User-Agent": user_agent()})  # noqa: S310 - fixed loopback http
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed loopback http
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def server_version() -> str | None:
    body = api_get("/api/version")
    return str(body["version"]) if body and "version" in body else None


def model_present() -> bool:
    body = api_get("/api/tags") or {}
    names = {str(m.get("name", "")) for m in body.get("models") or [] if isinstance(m, dict)}
    return EMBED_MODEL in names or f"{EMBED_MODEL}:latest" in names


def _services(ctx, verb: str, family: str) -> subprocess.CompletedProcess:
    """`scripts/heading-services.py <verb> <family>` of the installed engine."""
    return subprocess.run([str(ctx.python), str(ctx.engine / "scripts" / "heading-services.py"), verb, family],
                          cwd=str(ctx.engine), env=ctx.env(), capture_output=True, text=True, errors="replace")


def family_running(ctx, family: str) -> bool:
    return ctx.python.exists() and _services(ctx, "status", family).returncode == 0


def binary_ok() -> bool:
    """The versioned binary starts and reports OLLAMA_VERSION: present is not proof, a corrupt one is present too.

    `ollama --version` names the client version whether or not a server answers
    ("client version is X" beside a warning, else "ollama version is X");
    UNVERIFIED against 0.40.13 itself on 2026-11-01, so a binary wording it
    differently reads as "not in place" and is reinstalled, the safe direction.
    """
    binary = ollama_binary()
    if not (binary.is_file() and os.access(binary, os.X_OK)):
        return False
    try:
        probe = subprocess.run([str(binary), "--version"], capture_output=True, text=True, errors="replace",
                               timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return OLLAMA_VERSION in probe.stdout + probe.stderr


def check_server(ctx) -> bool:
    return binary_ok() and family_running(ctx, SERVER_FAMILY) and server_version() == OLLAMA_VERSION


def check_model(ctx) -> bool:
    # Our own server first: another ollama on this port listing bge-m3 is not ours.
    return check_server(ctx) and model_present()


def _stores(engine_root: Path, data_root: Path | None) -> list[tuple[str, Path]]:
    """(store, path) for the content store (under the data root) and the code store (engine).

    The first item names the physical STORE, not a query collection: the content
    store also holds the `history` and `chronicle` layers, the code store holds
    `commit-engine` beside `rule` and `skill` (config/memory-index.yaml).
    """
    stores = []
    if data_root is not None:
        from scripts.utils.runtime_dir import runtime_root  # engine environment only
        stores.append(("content", runtime_root(data_root) / "memory-index" / "cache" / "index.db"))
    stores.append(("code", engine_root / CODE_STORE_REL))
    return stores


def _read_only(store: Path):
    import sqlite3
    from contextlib import closing
    return closing(sqlite3.connect(f"{store.resolve().as_uri()}?mode=ro", uri=True))


def chunk_count(engine_root: Path, data_root: Path | None) -> int:
    """Rows in the `notes` table across both stores; 0 for a store that is absent."""
    total = 0
    for _, store in _stores(engine_root, data_root):
        if store.is_file():
            with _read_only(store) as conn:
                total += conn.execute("SELECT count(*) FROM notes").fetchone()[0]
    return total


def sample_chunk(engine_root: Path, data_root: Path | None) -> dict | None:
    """One indexed chunk with real text, content store first: {store, layer, path, chunk, text}.

    Deterministic (the first by path and chunk, long bodies first), so two
    doctor runs ask the same question; None only when no store holds any text.
    On a fresh install the content store holds one row, the data repository's
    first commit, in layer `commit-data` (measured on a fresh guest, HOS-312,
    2026-11-03). Which collection a layer is queried under is memory-index's
    decision, so the caller resolves it from `layer`, never from `store`.
    """
    for store_name, store in _stores(engine_root, data_root):
        if not store.is_file():
            continue
        with _read_only(store) as conn:
            # A body of 200 characters or more is preferred, as a sharper question;
            # any non-empty one still counts, so an index of short chunks is not "empty".
            row = conn.execute("SELECT path, chunk, body, layer FROM notes WHERE length(trim(body)) > 0 "
                               "ORDER BY length(body) >= 200 DESC, path, chunk LIMIT 1").fetchone()
        if row:
            return {"store": store_name, "layer": row[3], "path": row[0], "chunk": int(row[1]),
                    "text": row[2][:4000]}
    return None


def index_chunks(ctx) -> int:
    """`chunk_count` asked through the installed engine's interpreter, which resolves the store."""
    code = ("import sys; from pathlib import Path; from scripts.utils.install_index import chunk_count; "
            "print(chunk_count(Path(sys.argv[1]), Path(sys.argv[2])))")
    proc = subprocess.run([str(ctx.python), "-c", code, str(ctx.engine), str(ctx.data)], cwd=str(ctx.engine),
                          env=ctx.env(), capture_output=True, text=True, errors="replace")
    try:
        return int(proc.stdout.strip().splitlines()[-1]) if proc.returncode == 0 else 0
    except (ValueError, IndexError):
        return 0


def check_index(ctx) -> bool:
    return ctx.python.exists() and index_chunks(ctx) >= 1 and family_running(ctx, REFRESH_FAMILY)


# ============================================================
# Doing it
# ============================================================

def _fetch(url: str, start: int):
    if not url.startswith("https://"):
        raise InstallError(f"refusing a non-https download: {url}")
    from scripts.utils.user_agent import user_agent  # stdlib only
    headers = {"User-Agent": user_agent()}
    if start:
        headers["Range"] = f"bytes={start}-"
    req = urllib.request.Request(url, headers=headers)  # noqa: S310 - https only, checked above
    return urllib.request.urlopen(req, timeout=60)  # noqa: S310 - https only, checked above


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(_CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def download(url: str, dest: Path, digest: str) -> None:
    """`url` into `dest`, verified; an interrupted download resumes from `dest`.part on the next run."""
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    try:
        with _fetch(url, have) as resp:
            mode = "ab" if have and resp.status == 206 else "wb"  # a server ignoring Range sends it all again
            with part.open(mode) as out:
                shutil.copyfileobj(resp, out, _CHUNK)
    except urllib.error.HTTPError as exc:
        # 416: the part already holds every byte (a run stopped before verifying it).
        if not (have and exc.code == 416):
            raise
    if _sha256(part) != digest:
        part.unlink()
        raise InstallError(f"{dest.name} does not match its published checksum; it was removed, "
                           "run this again to download it afresh")
    os.replace(part, dest)


def _extract(archive: Path, into: Path) -> None:
    if not hasattr(tarfile, "data_filter"):
        raise InstallError("this Python cannot unpack archives safely; install ollama yourself")
    if archive.name.endswith(".tgz"):
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(into, filter="data")
        return
    zstd = shutil.which("zstd")
    if not zstd:
        hint = ("sudo apt-get install -y zstd" if shutil.which("apt-get")
                else "sudo dnf install -y zstd" if shutil.which("dnf") else "install zstd with your package manager")
        raise InstallError(f"the ollama archive needs the zstd tool to unpack. Run: {hint}, then run this again")
    proc = subprocess.Popen([zstd, "-q", "-d", "-c", "--", str(archive)], stdout=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            tar.extractall(into, filter="data")
    finally:
        proc.stdout.close()
        rc = proc.wait()
    if rc != 0:
        raise InstallError(f"zstd exited {rc} unpacking {archive.name}")


def install_binary() -> None:
    """Ollama OLLAMA_VERSION into ollama_home()/<version>, checksum-verified before unpacking."""
    asset = OLLAMA_SHA256.get((_system(), _arch() or ""))
    if not asset:
        raise InstallError(f"no ollama build for {platform.system()} on {platform.machine()}")
    name, digest = asset
    home = ollama_home()
    home.mkdir(parents=True, exist_ok=True)
    archive = home / name
    if not (archive.is_file() and _sha256(archive) == digest):
        print(f"  downloading ollama {OLLAMA_VERSION} ({name}); an interrupted download resumes on the next run")
        download(OLLAMA_RELEASE + name, archive, digest)
    target = home / OLLAMA_VERSION
    with tempfile.TemporaryDirectory(dir=home) as tmp:
        _extract(archive, Path(tmp) / "x")
        if target.exists():
            shutil.rmtree(target)
        os.replace(Path(tmp) / "x", target)
    archive.unlink()  # unpacked and verified; the archive is only a download cache
    if not binary_ok():
        raise InstallError(f"ollama {OLLAMA_VERSION} was unpacked but does not start or reports another version")


def _wait_version(seconds: int = 60) -> str | None:
    deadline = time.monotonic() + seconds
    while True:
        found = server_version()
        if found == OLLAMA_VERSION or time.monotonic() > deadline:
            return found
        time.sleep(1)


def _install_family(ctx, family: str) -> None:
    proc = _services(ctx, "install", family)
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-10:])
        raise InstallError(f"the service layer did not install {family}" + (f"\n{tail}" if tail else ""))


def install_server(ctx) -> None:
    if not binary_ok():  # absent, corrupt or another version: unpacked again from the verified archive
        install_binary()
    _install_family(ctx, SERVER_FAMILY)
    found = _wait_version()
    if found != OLLAMA_VERSION:
        # An older unit still running keeps its process through `enable --now`;
        # remove and install again through the service layer to restart it.
        _services(ctx, "remove", SERVER_FAMILY)
        _install_family(ctx, SERVER_FAMILY)
        found = _wait_version()
    if found != OLLAMA_VERSION:
        raise InstallError(f"another program answers on {OLLAMA_ADDRESS}" + (f" (ollama {found})" if found else "")
                           + " instead of this install's ollama; stop it, then run this again")


def pull_model(ctx) -> None:
    # Ollama keeps the layers it already has, so a rerun continues a partial pull.
    proc = subprocess.run([str(ollama_binary()), "pull", EMBED_MODEL], env=_client_env(ctx))
    if proc.returncode != 0:
        raise InstallError(f"`ollama pull {EMBED_MODEL}` exited {proc.returncode}; run this again to continue it")


def build_index(ctx) -> None:
    print("  building the index for the first time; later changes are picked up every hour")
    proc = subprocess.run([str(ctx.python), str(ctx.engine / "scripts" / "memory-index.py"), "build"],
                          cwd=str(ctx.engine), env=ctx.env())
    if proc.returncode != 0:
        raise InstallError(f"the index build exited {proc.returncode}; run this again, it resumes")
    if index_chunks(ctx) < 1:
        raise InstallError("the index build finished but the index holds nothing")
    _install_family(ctx, REFRESH_FAMILY)
