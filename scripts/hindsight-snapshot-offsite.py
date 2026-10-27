#!/usr/bin/env python3
"""Keep an encrypted, verified copy of the Hindsight snapshot off this machine, on Google Drive.

    hindsight-snapshot-offsite.py authorize      # once, by hand: browser consent for drive.file; HELM only
    hindsight-snapshot-offsite.py run            # archive, encrypt, upload, verify, prune; HELM only
    hindsight-snapshot-offsite.py status [--remote]   # last verified copy; --remote lists the Drive folder
    hindsight-snapshot-offsite.py self-test      # a fake Drive and an invented snapshot; nothing leaves the machine

Why. Operator decision, 2026-10-07 (HOS-210): Google Drive may hold it, a script
makes the daily copy, to his personal Gmail, as a separate task (translated). The snapshot under
`runtime:hindsight-snapshot/cache` (`hindsight-snapshot.py take`) holds what lives only in
the Hindsight bank; this sends it off the machine, and only as ciphertext.

One run:

1. Under the snapshot's own lock, `current/` is written as a tar into `zstd -19`, in
   scratch (`runtime:scratch/tmp`), to `<name>.tar.zst.tmp` and renamed when complete.
2. `gpg --symmetric` (AES256, passphrase read by gpg itself from a 0600 file) writes
   `<name>.tar.zst.gpg.tmp`, renamed when complete. The plaintext archive is then removed.
3. The ciphertext is uploaded into ONE folder of the Drive the token was granted on,
   named `<name>.tar.zst.gpg.pending`, carrying `appProperties` that mark it ours.
4. It is downloaded back, its sha256 compared with what was sent, decrypted (gpg's integrity
   check refuses altered ciphertext), and handed to `hindsight-snapshot.py restore-check`.
   Only when that exits 0 is the copy renamed to its final name and marked `verified`.
   A copy that fails any step is moved to the Drive trash and the last verified copy stays.
5. Retention: verified copies are kept as restic's `--keep-daily 7 --keep-weekly 4` keeps
   them (the newest copy of each of the 7 most recent days that have one, and of each of the
   4 most recent ISO weeks that have one; a copy can satisfy both). The rest are moved to the
   Drive trash, which keeps them 30 days. Only files in our folder, with our name pattern AND
   our `appProperties` mark, are ever touched, and only when a NEWER verified copy exists.

The token. Its own file, `.sessions/google/drive_snapshot_token.json` in the engine clone,
scope `drive.file` only (the app sees the files it created and nothing else), minted from the
OAuth client `scripts/utils/gmail_auth.py` already uses, through that module's
`google_credentials()`. `run` never opens a browser: without a usable token it fails and says
so, and `authorize` is the one command that asks for consent.

Settings, from the gitignored `.env`:
    HINDSIGHT_OFFSITE_PASSPHRASE_FILE   required; a 0600 file owned by this user
    HINDSIGHT_OFFSITE_FOLDER            optional; the Drive folder name (default below)

A failure is reported to the operator's own sink through `scripts/utils/telegram_notify.py`,
once per change from ok to failing, and the run exits 1 so the unit shows `failed`.

The second kind: machine-config (HOS-219). Operator decision, 2026-10-09: all of it may sit on the
same external Google Drive as the memory backup, with something like a Readme.md beside
each file (translated), scope "1, 2 and 3", secrets "B". The same `run` then
copies the files of this machine that live in no repository, listed in the manifest under
registry name `machine-config-manifest` (each entry: source, file or dir, secret or not, what
it is, where it goes back, the restore steps). The archive carries each file or directory
under `machine-config/<id>/`, a `<name>.README.md` beside it generated from the manifest
(size, sha256, mode, owner, links, the restore steps; never a value), and one `RESTORE.md`.
It goes through steps 2 to 5 above with its own name, `machine-config-<stamp>.tar.zst.gpg`,
so each kind's retention sees only its own copies. Instead of `restore-check`, the copy
downloaded back is decrypted and streamed: every member's sha256 must equal the bytes read
from the source at archive time, and every entry that existed must have its README and be
named in RESTORE.md. Secret plaintext exists only in memory and in the 0700 scratch
directory of the run, which is removed. Its state file is its own, so a failure of either
kind is reported and alarmed on its own and never skips the other.
"""
from __future__ import annotations

import argparse
import contextlib
import fnmatch
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.utils.api import load_api_key  # noqa: E402
from scripts.utils.atomic import atomic_write_text  # noqa: E402
from scripts.utils.data_path import data_write_dir  # noqa: E402

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
TOKEN = ROOT / ".sessions" / "google" / "drive_snapshot_token.json"
DEFAULT_FOLDER = "HEADING OS - Hindsight snapshot"
MARK_KEY, MARK_VALUE = "heading_os", "hindsight-snapshot"
FOLDER_MIME = "application/vnd.google-apps.folder"
NAME_RE = re.compile(r"^hindsight-snapshot-(\d{8}T\d{6}Z)\.tar\.zst\.gpg(\.pending)?$")
STATE_FILE = "offsite-state.json"   # beside current/, in the gitignored snapshot cache
KEEP_DAILY, KEEP_WEEKLY = 7, 4
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"
SNAPSHOT_SCRIPT = ROOT / "scripts" / "hindsight-snapshot.py"
RESTORE_CHECK_TIMEOUT_S = 1800
CHUNK = 8 * 1024 * 1024
GPG_BASE = ["gpg", "--batch", "--yes", "--quiet", "--no-symkey-cache", "--pinentry-mode", "loopback"]
MC_NAME_RE = re.compile(r"^machine-config-(\d{8}T\d{6}Z)\.tar\.zst\.gpg(\.pending)?$")
MC_STATE_FILE = "offsite-machine-config-state.json"   # beside STATE_FILE
MC_ROOT = "machine-config"                            # the top directory inside the archive
MC_ALWAYS_EXCLUDE = ("*.bak*",)
MC_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class OffsiteError(RuntimeError):
    """A step of the off-machine copy did not complete; the last verified copy stands."""


def snapshot_module():
    """`scripts/hindsight-snapshot.py`, loaded by path: its lock and layout are used as they are."""
    spec = importlib.util.spec_from_file_location("hindsight_snapshot_for_offsite", SNAPSHOT_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ============================================================
# Local: archive, encrypt, decrypt
# ============================================================


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def check_passphrase_file(path: Path) -> Path:
    """The file gpg reads the passphrase from: a regular, non-empty file of this user, mode 0600 or narrower."""
    try:
        st = path.stat()
    except OSError as exc:
        raise OffsiteError(f"passphrase file {path}: {exc.strerror}") from exc
    if not stat.S_ISREG(st.st_mode):
        raise OffsiteError(f"passphrase file {path} is not a regular file")
    if st.st_uid != os.getuid():
        raise OffsiteError(f"passphrase file {path} is not owned by this user")
    if st.st_mode & 0o077:
        raise OffsiteError(f"passphrase file {path} is mode {oct(st.st_mode & 0o777)}; it must be 0600")
    if st.st_size == 0:
        raise OffsiteError(f"passphrase file {path} is empty")
    return path


def build_archive(current: Path, dest: Path) -> None:
    """`current/` as a tar through `zstd -19`, written to `dest.tmp` and renamed when whole."""
    tmp = dest.with_name(dest.name + ".tmp")
    proc = subprocess.Popen(["zstd", "-q", "-19", "-T0", "-f", "-o", str(tmp), "-"], stdin=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=proc.stdin, mode="w|") as tf:
            tf.add(current, arcname="current")
    finally:
        proc.stdin.close()
        rc = proc.wait()
    if rc != 0:
        tmp.unlink(missing_ok=True)
        raise OffsiteError(f"zstd exited {rc}")
    os.replace(tmp, dest)


def _gpg(args: list[str], dest: Path) -> None:
    tmp = dest.with_name(dest.name + ".tmp")
    proc = subprocess.run([*GPG_BASE, *args[:2], "-o", str(tmp), *args[2:]],
                          capture_output=True, text=True, timeout=1800, check=False)
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise OffsiteError(f"gpg exited {proc.returncode}: {(proc.stderr or '').strip()[:300]}")
    os.replace(tmp, dest)


def encrypt(src: Path, dest: Path, passphrase_file: Path) -> None:
    _gpg(["--passphrase-file", str(passphrase_file), "--symmetric", "--cipher-algo", "AES256",
          "--s2k-mode", "3", "--s2k-digest-algo", "SHA512", "--s2k-count", "65011712",
          "--compress-algo", "none", "--", str(src)], dest)


def decrypt(src: Path, dest: Path, passphrase_file: Path) -> None:
    _gpg(["--passphrase-file", str(passphrase_file), "--decrypt", "--", str(src)], dest)


def restore_check(archive: Path, scratch: Path) -> tuple[bool, str]:
    """The snapshot's own `restore-check`, run as a command, judged by its exit code."""
    proc = subprocess.run([str(VENV_PYTHON), str(SNAPSHOT_SCRIPT), "restore-check", str(archive),
                           "--scratch", str(scratch)],
                          capture_output=True, text=True, timeout=RESTORE_CHECK_TIMEOUT_S, check=False)
    return proc.returncode == 0, ((proc.stdout or "") + (proc.stderr or "")).strip()


# ============================================================
# Drive
# ============================================================


class GoogleDrive:
    """The six Drive calls this needs, over a `drive` v3 service. Every call reaches only what the
    `drive.file` token can see: files this app created."""

    FIELDS = "id,name,size,md5Checksum,appProperties,parents,createdTime"

    def __init__(self, service):
        self.s = service

    @staticmethod
    def _quote(text: str) -> str:
        return text.replace("\\", "\\\\").replace("'", "\\'")

    def find_folders(self, name: str) -> list[dict]:
        q = (f"mimeType='{FOLDER_MIME}' and name='{self._quote(name)}' and trashed=false "
             f"and appProperties has {{ key='{MARK_KEY}' and value='{MARK_VALUE}' }}")
        return self.s.files().list(q=q, spaces="drive", fields="files(id,name)").execute(num_retries=5)["files"]

    def create_folder(self, name: str) -> str:
        body = {"name": name, "mimeType": FOLDER_MIME, "appProperties": {MARK_KEY: MARK_VALUE}}
        return self.s.files().create(body=body, fields="id").execute(num_retries=5)["id"]

    def list(self, folder_id: str) -> list[dict]:
        out, token = [], None
        while True:
            r = self.s.files().list(q=f"'{folder_id}' in parents and trashed=false", spaces="drive",
                                    fields=f"nextPageToken,files({self.FIELDS})", pageSize=1000,
                                    pageToken=token).execute(num_retries=5)
            out += r.get("files", [])
            token = r.get("nextPageToken")
            if not token:
                return out

    def upload(self, folder_id: str, path: Path, name: str, props: dict) -> dict:
        from googleapiclient.http import MediaFileUpload

        media = MediaFileUpload(str(path), mimetype="application/octet-stream", resumable=True, chunksize=CHUNK)
        req = self.s.files().create(body={"name": name, "parents": [folder_id], "appProperties": props},
                                    media_body=media, fields=self.FIELDS)
        resp = None
        while resp is None:
            _, resp = req.next_chunk(num_retries=5)
        return resp

    def download(self, file_id: str, dest: Path) -> None:
        from googleapiclient.http import MediaIoBaseDownload

        with open(dest, "wb") as fh:
            dl = MediaIoBaseDownload(fh, self.s.files().get_media(fileId=file_id), chunksize=CHUNK)
            done = False
            while not done:
                _, done = dl.next_chunk(num_retries=5)

    def update(self, file_id: str, name: str, props: dict) -> dict:
        return self.s.files().update(fileId=file_id, body={"name": name, "appProperties": props},
                                     fields=self.FIELDS).execute(num_retries=5)

    def trash(self, file_id: str) -> None:
        self.s.files().update(fileId=file_id, body={"trashed": True}, fields="id").execute(num_retries=5)


def drive_service(interactive: bool):
    from googleapiclient.discovery import build

    from scripts.utils.gmail_auth import google_credentials
    from scripts.utils.google_user_agent import google_http

    creds = google_credentials(SCOPES, str(TOKEN), interactive=interactive)
    return GoogleDrive(build("drive", "v3", http=google_http(creds)))


def ensure_folder(drive, name: str) -> str:
    found = drive.find_folders(name)
    if len(found) > 1:
        raise OffsiteError(f"{len(found)} folders named {name!r} carry our mark; refusing to guess which")
    return found[0]["id"] if found else drive.create_folder(name)


# ============================================================
# Retention
# ============================================================


def ours(f: dict, folder_id: str, name_re: re.Pattern = NAME_RE) -> re.Match | None:
    """The name match of a file this tool made in this folder, or None for anything else.
    `name_re` is one kind's pattern, so a kind's retention never sees the other kind's copies."""
    if (f.get("appProperties") or {}).get(MARK_KEY) != MARK_VALUE or folder_id not in (f.get("parents") or []):
        return None
    return name_re.match(str(f.get("name") or ""))


def to_prune(files: list[dict], folder_id: str, name_re: re.Pattern = NAME_RE) -> list[dict]:
    """Our copies to move to the trash: verified ones outside 7 daily + 4 weekly, and pending ones
    older than the newest verified copy. Nothing at all when no verified copy exists."""
    copies = []
    for f in files:
        m = ours(f, folder_id, name_re)
        if m:
            verified = not m.group(2) and f["appProperties"].get("state") == "verified"
            copies.append((m.group(1), verified, f))
    verified = sorted((c for c in copies if c[1]), key=lambda c: c[0], reverse=True)
    if not verified:
        return []
    newest = verified[0][0]
    keep, days, weeks = set(), [], []
    for stamp, _, f in verified:
        day = stamp[:8]
        if day not in days and len(days) < KEEP_DAILY:
            days.append(day)
            keep.add(f["id"])
    for stamp, _, f in verified:
        week = date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:8])).isocalendar()[:2]
        if week not in weeks and len(weeks) < KEEP_WEEKLY:
            weeks.append(week)
            keep.add(f["id"])
    return [f for stamp, _, f in copies if f["id"] not in keep and stamp < newest]


# ============================================================
# run
# ============================================================


def load_state(base: Path, state_file: str = STATE_FILE) -> dict:
    try:
        return json.loads((base / state_file).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        print(f"offsite: {state_file} is unreadable ({exc}); treating as no state", file=sys.stderr)
        return {}


def save_state(base: Path, state: dict, state_file: str = STATE_FILE) -> None:
    atomic_write_text(base / state_file, json.dumps(state, ensure_ascii=False, indent=1) + "\n", mode=0o600)


def ship(drive, work: Path, enc: Path, name: str, extra_props: dict, passphrase_file: Path,
         folder_name: str, name_re: re.Pattern, check) -> dict:
    """Steps 3 to 5 for one encrypted archive of either kind: upload as pending, download back,
    compare, decrypt, hand the plaintext to `check` (which raises OffsiteError to refuse), then
    rename to verified and prune that kind's copies by `name_re`. Returns the copy's record."""
    sha, size = sha256_of(enc), enc.stat().st_size
    md5 = hashlib.md5(enc.read_bytes(), usedforsecurity=False).hexdigest()
    folder = ensure_folder(drive, folder_name)
    props = {MARK_KEY: MARK_VALUE, "state": "pending", "sha256": sha, **extra_props}
    meta = drive.upload(folder, enc, name + ".pending", props)
    try:
        if int(meta.get("size", -1)) != size or meta.get("md5Checksum") != md5:
            raise OffsiteError(f"Drive reports size {meta.get('size')} md5 {meta.get('md5Checksum')}, "
                               f"sent {size} bytes md5 {md5}")
        back = work / "downloaded.gpg"
        drive.download(meta["id"], back)
        if sha256_of(back) != sha:
            raise OffsiteError("the copy downloaded back differs from what was sent")
        restored = work / "restored.tar.zst"
        decrypt(back, restored, passphrase_file)
        check(restored)
    except BaseException:
        try:
            drive.trash(meta["id"])   # ours, never verified; the last verified copy is untouched
        except Exception as exc:  # noqa: BLE001 - said, and the original failure is what propagates
            print(f"offsite: the unverified copy {meta['id']} could not be moved to the trash "
                  f"({type(exc).__name__}: {exc}); the next good run prunes it", file=sys.stderr)
        raise
    drive.update(meta["id"], name, {**props, "state": "verified"})
    pruned = to_prune(drive.list(folder), folder, name_re)
    for f in pruned:
        drive.trash(f["id"])
    return {"name": name, "id": meta["id"], "bytes": size, "sha256": sha, "pruned": [f["name"] for f in pruned]}


def run(drive, base: Path, work: Path, passphrase_file: Path, folder_name: str, now: datetime) -> dict:
    """One copy, accepted only after it came back whole. Returns the verified copy's record."""
    snap = snapshot_module()
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"hindsight-snapshot-{stamp}.tar.zst.gpg"
    work = Path(tempfile.mkdtemp(prefix="offsite-", dir=work))
    plain, enc = work / f"hindsight-snapshot-{stamp}.tar.zst", work / name
    try:
        with snap.locked(base):
            manifest = snap.manifest_of(base)
            if manifest is None:
                raise OffsiteError(f"no whole snapshot at {base / snap.CURRENT} (no {snap.MANIFEST})")
            build_archive(base / snap.CURRENT, plain)
        encrypt(plain, enc, passphrase_file)
        plain.unlink()

        def check(restored: Path) -> None:
            ok, report = restore_check(restored, work)
            if not ok:
                raise OffsiteError(f"restore-check refused the copy: {report[-400:]}")

        rec = ship(drive, work, enc, name, {"taken_at": str(manifest.get("taken_at"))[:25]}, passphrase_file,
                   folder_name, NAME_RE, check)
        return {**rec, "snapshot_taken_at": manifest.get("taken_at"),
                "verified_at": now.isoformat(timespec="seconds")}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ============================================================
# machine-config: the files of this machine that live in no repository (HOS-219)
# ============================================================


def load_machine_manifest(path: Path) -> dict:
    """The manifest, checked for the fields every entry must carry. Refuses rather than guesses."""
    from scripts.utils import yamlio

    try:
        doc = yamlio.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OffsiteError(f"machine-config manifest {path} does not exist") from exc
    except Exception as exc:  # noqa: BLE001 - any parse failure is a refused manifest, said with its type
        raise OffsiteError(f"machine-config manifest {path} is unreadable: {type(exc).__name__}") from exc
    entries = (doc or {}).get("entries") if isinstance(doc, dict) else None
    if not isinstance(entries, list) or not entries:
        raise OffsiteError(f"machine-config manifest {path} has no entries")
    seen = set()
    for i, e in enumerate(entries):
        where = f"machine-config manifest entry {i + 1}"
        if not isinstance(e, dict):
            raise OffsiteError(f"{where} is not a mapping")
        for key in ("id", "source", "what", "restore_to"):
            if not isinstance(e.get(key), str) or not e[key].strip():
                raise OffsiteError(f"{where} has no {key}")
        if not MC_ID_RE.match(e["id"]) or e["id"] in seen:
            raise OffsiteError(f"{where}: id {e['id']!r} is not a unique lowercase slug")
        seen.add(e["id"])
        if e.get("kind") not in ("file", "dir"):
            raise OffsiteError(f"{where} ({e['id']}): kind must be file or dir")
        if not isinstance(e.get("secret"), bool):
            raise OffsiteError(f"{where} ({e['id']}): secret must be true or false")
        steps = e.get("restore_steps")
        if not isinstance(steps, list) or not steps or not all(isinstance(s, str) for s in steps):
            raise OffsiteError(f"{where} ({e['id']}): restore_steps must be a non-empty list of text")
    return doc


def expand_source(text: str, engine: Path) -> Path:
    return Path(text.replace("{engine}", str(engine))).expanduser()


def tracked_paths(repos: list[Path]) -> set[Path]:
    """Every file git tracks in the given repositories, as absolute paths. A repository that
    cannot be asked is said, and then nothing under it is excluded on that ground."""
    out: set[Path] = set()
    for repo in repos:
        proc = subprocess.run(["git", "-C", str(repo), "ls-files", "-z"], capture_output=True, timeout=120,
                              check=False)
        if proc.returncode != 0:
            print(f"offsite: could not list the files git tracks in {repo}; none skipped as tracked there",
                  file=sys.stderr)
            continue
        out.update(repo / p for p in proc.stdout.decode("utf-8", "surrogateescape").split("\0") if p)
    return out


def _excluded(rel: Path, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(part, pat) or fnmatch.fnmatch(rel.as_posix(), pat)
               for pat in patterns for part in rel.parts)


def _owner(st: os.stat_result) -> str:
    import grp
    import pwd

    try:
        return f"{pwd.getpwuid(st.st_uid).pw_name}:{grp.getgrgid(st.st_gid).gr_name}"
    except KeyError:
        return f"{st.st_uid}:{st.st_gid}"


def collect_entry(entry: dict, engine: Path, tracked: set[Path], global_exclude: list[str]) -> dict:
    """What one manifest entry holds right now: each regular file's bytes, read ONCE, with its
    hash, size, mode, owner and mtime; the symlinks with their targets; what was skipped and why.
    Never follows a link. A source that is not there comes back with `present` False."""
    src = expand_source(entry["source"], engine)
    rec = {"entry": entry, "src": src, "present": False, "files": [], "links": [], "skipped": []}
    if not os.path.lexists(src):
        return rec
    patterns = [*MC_ALWAYS_EXCLUDE, *global_exclude, *(entry.get("exclude") or [])]

    def take(path: Path, rel: Path) -> None:
        st = path.lstat()
        if stat.S_ISLNK(st.st_mode):
            rec["links"].append((rel.as_posix(), os.readlink(path)))
        elif not stat.S_ISREG(st.st_mode):
            rec["skipped"].append((rel.as_posix(), "not a regular file"))
        elif path in tracked or path.resolve() in tracked:
            rec["skipped"].append((rel.as_posix(), "tracked in a repository"))
        else:
            data = path.read_bytes()
            rec["files"].append({"rel": rel.as_posix(), "data": data, "sha256": hashlib.sha256(data).hexdigest(),
                                 "size": len(data), "mode": stat.S_IMODE(st.st_mode), "owner": _owner(st),
                                 "mtime": int(st.st_mtime)})

    if entry["kind"] == "file":
        if src.is_symlink() or not src.is_file():
            rec["skipped"].append((src.name, "manifest says file, the source is not a regular file"))
        else:
            take(src, Path(src.name))
        rec["present"] = True
        return rec
    if not src.is_dir() or src.is_symlink():
        rec["skipped"].append((src.name, "manifest says dir, the source is not a directory"))
        rec["present"] = True
        return rec
    rec["present"] = True
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        d = Path(dirpath)
        for name in sorted(dirnames):
            rel = (d / name).relative_to(src)
            if (d / name).is_symlink():
                take(d / name, rel)
            elif _excluded(rel, patterns):
                rec["skipped"].append((rel.as_posix() + "/", "excluded by pattern"))
        dirnames[:] = sorted(n for n in dirnames
                             if not (d / n).is_symlink() and not _excluded((d / n).relative_to(src), patterns))
        for name in sorted(filenames):
            rel = (d / name).relative_to(src)
            if _excluded(rel, patterns):
                rec["skipped"].append((rel.as_posix(), "excluded by pattern"))
            else:
                take(d / name, rel)
    return rec


def _arc_base(rec: dict) -> str:
    """Where an entry sits in the archive: `machine-config/<id>/<source name>`."""
    return f"{MC_ROOT}/{rec['entry']['id']}/{rec['src'].name}"


def _enable_hint(rel: str) -> str:
    parent = Path(rel).parent.name
    if parent.endswith((".wants", ".requires")):
        return f"  (enable with `systemctl --user enable {Path(rel).name}`)"
    return ""


def render_readme(rec: dict, taken_at: str, host: str) -> str:
    """The README beside one entry. Paths, sizes, hashes, modes and the manifest's own words;
    never a byte of any file's content."""
    e = rec["entry"]
    files = rec["files"]
    lines = [f"# {rec['src'].name}", "", e["what"].strip(), "",
             f"- Manifest entry: `{e['id']}`",
             f"- Kind: {'one file' if e['kind'] == 'file' else f'directory, {len(files)} file(s) copied'}",
             f"- Source path: `{e['source']}` (`{rec['src']}` on {host})",
             f"- Taken at: {taken_at}",
             "- Secret: " + ("YES. This holds credentials. Never print it, never commit it, keep it mode 600."
                             if e["secret"] else "no"),
             f"- Goes back to: `{e['restore_to']}`",
             f"- In this archive: `{_arc_base(rec)}`", "", "## Files", ""]
    if files:
        lines += ["| path | size | sha256 | mode | owner |", "|---|---|---|---|---|"]
        lines += [f"| `{f['rel']}` | {f['size']} | `{f['sha256']}` | {oct(f['mode'])[2:]} | {f['owner']} |"
                  for f in files]
    else:
        lines.append("None copied.")
    if rec["links"]:
        lines += ["", "## Links (recorded, not copied; recreate them)", ""]
        lines += [f"- `{rel}` -> `{target}`{_enable_hint(rel)}" for rel, target in rec["links"]]
    if rec["skipped"]:
        lines += ["", "## Skipped", ""] + [f"- `{rel}`: {why}" for rel, why in rec["skipped"]]
    lines += ["", "## Restore", ""] + [f"{i}. {s.strip()}" for i, s in enumerate(e["restore_steps"], 1)]
    return "\n".join(lines) + "\n"


def render_restore(doc: dict, recs: list[dict], stamp: str, taken_at: str, host: str,
                   passphrase_file: Path) -> str:
    """RESTORE.md at the archive root: how to decrypt, the order, and every manifest entry."""
    name = f"machine-config-{stamp}.tar.zst.gpg"
    lines = ["# Restoring this machine", "",
             f"Archive `{name}`, taken {taken_at} on {host} by `scripts/hindsight-snapshot-offsite.py run`.",
             "It sits in the same Google Drive folder as the Hindsight snapshot copies.", "",
             "## Decrypting", "",
             "The archive is gpg symmetric (AES256). gpg reads the passphrase from a file; on the machine "
             f"this came from, that file was `{passphrase_file}`, named by `HINDSIGHT_OFFSITE_PASSPHRASE_FILE` "
             "in the engine clone's `.env`.", ""]
    if isinstance(doc.get("passphrase"), str):
        lines += [doc["passphrase"].strip(), ""]
    lines += ["Recreate the passphrase file with mode 600, then:", "", "```",
              f"gpg --batch --pinentry-mode loopback --passphrase-file <the passphrase file> -o machine-config.tar.zst --decrypt {name}",
              "zstd -d -c machine-config.tar.zst | tar -x -p",
              "```", "",
              f"Everything lands under `{MC_ROOT}/<entry id>/`, each file or directory beside its `.README.md`.",
              "Remove the decrypted copy when done: it holds the secrets in plain text.", "",
              "## Order", ""]
    steps = [*(doc.get("before") or []),
             "Restore each entry below, in the order listed, following its README.",
             *(doc.get("after") or [])]
    lines += [f"{i}. {s.strip()}" for i, s in enumerate(steps, 1)]
    lines += ["", "## Entries", ""]
    for rec in recs:
        e = rec["entry"]
        state = (f"{len(rec['files'])} file(s), README `{_arc_base(rec)}.README.md`" if rec["present"]
                 else "NOT PRESENT on the machine at archive time; nothing copied")
        lines.append(f"- `{e['id']}`{' (secret)' if e['secret'] else ''}: `{e['source']}` -> `{e['restore_to']}`. "
                     f"{state}. {e['what'].strip()}")
    return "\n".join(lines) + "\n"


def build_machine_archive(recs: list[dict], doc: dict, dest: Path, stamp: str, taken_at: str, host: str,
                          passphrase_file: Path) -> dict[str, str]:
    """The tar through `zstd -19`, built from the bytes already read, so what is hashed is what is
    archived. Returns every member's sha256 by name, which is what verification compares."""
    members: list[tuple[str, bytes, int, int]] = []
    for rec in recs:
        if not rec["present"]:
            continue
        base = _arc_base(rec)
        for f in rec["files"]:
            arc = base if rec["entry"]["kind"] == "file" else f"{base}/{f['rel']}"
            members.append((arc, f["data"], f["mode"], f["mtime"]))
        members.append((base + ".README.md", render_readme(rec, taken_at, host).encode(), 0o644, 0))
    restore = render_restore(doc, recs, stamp, taken_at, host, passphrase_file).encode()
    members.append((f"{MC_ROOT}/RESTORE.md", restore, 0o644, 0))
    tmp = dest.with_name(dest.name + ".tmp")
    proc = subprocess.Popen(["zstd", "-q", "-19", "-T0", "-f", "-o", str(tmp), "-"], stdin=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=proc.stdin, mode="w|", format=tarfile.PAX_FORMAT) as tf:
            for arc, data, mode, mtime in members:
                info = tarfile.TarInfo(arc)
                info.size, info.mode, info.mtime = len(data), mode, mtime
                tf.addfile(info, io.BytesIO(data))
    finally:
        proc.stdin.close()
        rc = proc.wait()
    if rc != 0:
        tmp.unlink(missing_ok=True)
        raise OffsiteError(f"zstd exited {rc}")
    os.replace(tmp, dest)
    return {arc: hashlib.sha256(data).hexdigest() for arc, data, _, _ in members}


def verify_machine_archive(restored: Path, expected: dict[str, str], recs: list[dict]) -> None:
    """Stream the decrypted archive, never extracting it: every member's sha256 equals the one
    taken at archive time, nothing is missing or extra, every entry that existed has its README,
    and RESTORE.md names every manifest entry. Errors name members, never their content."""
    proc = subprocess.Popen(["zstd", "-q", "-d", "-c", str(restored)], stdout=subprocess.PIPE)
    seen: dict[str, str] = {}
    restore_text = ""
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tf:
            for m in tf:
                if not m.isfile():
                    raise OffsiteError(f"the archive holds a non-file member {m.name!r}")
                h, fh = hashlib.sha256(), tf.extractfile(m)
                chunks = []
                while block := fh.read(CHUNK):
                    h.update(block)
                    if m.name == f"{MC_ROOT}/RESTORE.md":
                        chunks.append(block)
                seen[m.name] = h.hexdigest()
                if chunks:
                    restore_text = b"".join(chunks).decode("utf-8")
    finally:
        proc.stdout.close()
        rc = proc.wait()
    if rc != 0:
        raise OffsiteError(f"zstd -d exited {rc} on the decrypted copy")
    missing = sorted(set(expected) - set(seen))
    extra = sorted(set(seen) - set(expected))
    differ = sorted(a for a in set(expected) & set(seen) if expected[a] != seen[a])
    if missing or extra or differ:
        raise OffsiteError(f"the copy does not match the source at archive time: {len(missing)} missing, "
                           f"{len(extra)} extra, {len(differ)} differ; first: {(missing + extra + differ)[0]}")
    for rec in recs:
        e = rec["entry"]
        if rec["present"] and _arc_base(rec) + ".README.md" not in seen:
            raise OffsiteError(f"entry {e['id']} has no README in the copy")
        if f"`{e['id']}`" not in restore_text:
            raise OffsiteError(f"RESTORE.md does not name entry {e['id']}")


def run_machine_config(drive, manifest_path: Path, work: Path, passphrase_file: Path, folder_name: str,
                       now: datetime, engine: Path, repos: list[Path]) -> dict:
    """The machine-config copy: collect, archive, encrypt, then the same upload and download-back
    as the snapshot, verified by `verify_machine_archive`. Returns the verified copy's record."""
    doc = load_machine_manifest(manifest_path)
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    taken_at = now.isoformat(timespec="seconds")
    host = os.uname().nodename
    name = f"machine-config-{stamp}.tar.zst.gpg"
    tracked = tracked_paths(repos)
    recs = [collect_entry(e, engine, tracked, list(doc.get("exclude") or [])) for e in doc["entries"]]
    work = Path(tempfile.mkdtemp(prefix="offsite-mc-", dir=work))
    plain, enc = work / f"machine-config-{stamp}.tar.zst", work / name
    try:
        expected = build_machine_archive(recs, doc, plain, stamp, taken_at, host, passphrase_file)
        encrypt(plain, enc, passphrase_file)
        plain.unlink()
        rec = ship(drive, work, enc, name, {"kind": "machine-config", "taken_at": taken_at[:25]},
                   passphrase_file, folder_name, MC_NAME_RE, lambda restored: verify_machine_archive(
                       restored, expected, recs))
        return {**rec, "taken_at": taken_at, "verified_at": now.isoformat(timespec="seconds"),
                "entries": sum(1 for r in recs if r["present"]),
                "files": sum(len(r["files"]) for r in recs),
                "secret_files": sum(len(r["files"]) for r in recs if r["entry"]["secret"]),
                "missing": [r["entry"]["id"] for r in recs if not r["present"]],
                "skipped": sum(len(r["skipped"]) for r in recs)}
    finally:
        recs.clear()   # drop the read bytes, secrets among them, before returning
        shutil.rmtree(work, ignore_errors=True)


def alarm(base: Path, detail: str, send, state_file: str = STATE_FILE, label: str = "snapshot") -> None:
    """Record the failure; tell the operator's own sink only on the change from ok to failing.
    Each kind has its own state file, so one kind failing never silences the other's alarm."""
    print(f"offsite: {label}: FAILED: {detail}", file=sys.stderr)
    state = load_state(base, state_file)
    previous = state.get("state", "ok")
    save_state(base, {**state, "state": "failing", "last_error": detail[:500],
                      "failed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}, state_file)
    if previous == "failing":
        print(f"offsite: {label}: already reported; not notifying again", file=sys.stderr)
        return
    if not send(f"HEADING OS off-machine {label} copy FAILED; the last verified copy stands.\n\n{detail[:800]}"):
        print("offsite: NO NOTIFICATION SENT, no operator sink is configured or the transport refused.",
              file=sys.stderr)


def notify_own_sinks(text: str) -> bool:
    from scripts.utils.telegram_notify import notify, own_targets

    return sum(1 for t in sorted(own_targets()) if notify(t, text)) > 0


def _failures() -> tuple:
    """Every exception class a run can end in that is a failed copy rather than a bug."""
    import httplib2
    from google.auth.exceptions import GoogleAuthError
    from googleapiclient.errors import Error as GoogleApiError

    snap = snapshot_module()
    return (OffsiteError, snap.Incomplete, OSError, ValueError, subprocess.SubprocessError,
            tarfile.TarError, GoogleApiError, GoogleAuthError, httplib2.HttpLib2Error)


KINDS = (("snapshot", STATE_FILE), ("machine-config", MC_STATE_FILE))


def run_all(jobs: dict, base: Path, send, failures: tuple) -> int:
    """Run each kind's job in turn; a failure of one is alarmed and reported and the next still
    runs. `jobs` maps a kind's label to a callable returning its record, or to the exception that
    stopped the shared setup, which then counts as that kind's failure. Exit 1 if any failed."""
    rc = 0
    for label, state_file in KINDS:
        job = jobs[label]
        try:
            if isinstance(job, BaseException):
                raise job
            rec = job()
        except failures as exc:
            alarm(base, f"{type(exc).__name__}: {exc}", send, state_file, label)
            rc = 1
            continue
        save_state(base, {"state": "ok", "last_verified": rec}, state_file)
        print(f"offsite {label} copy verified: {rec['name']} ({rec['bytes']} bytes)")
        if label == "machine-config":
            print(f"  {rec['entries']} entries, {rec['files']} files ({rec['secret_files']} secret), "
                  f"{rec['skipped']} skipped; not on this machine: {', '.join(rec['missing']) or 'none'}")
        for n in rec["pruned"]:
            print(f"  moved to the Drive trash by retention: {n}")
    return rc


def cmd_run(_args) -> int:
    from scripts.utils.clone_guard import require_main_clone
    from scripts.utils.data_path import DataPathError, data_path
    from scripts.utils.gmail_auth import ConsentRequired
    from scripts.utils.paths import get_data_root

    require_main_clone(__file__)
    base = data_write_dir("runtime:hindsight-snapshot/cache")
    failures = (ConsentRequired, DataPathError, *_failures())
    now = datetime.now(timezone.utc)
    try:
        raw = load_api_key("HINDSIGHT_OFFSITE_PASSPHRASE_FILE", required=False)
        if not raw:
            raise OffsiteError("HINDSIGHT_OFFSITE_PASSPHRASE_FILE is not set in .env")
        passphrase = check_passphrase_file(Path(raw).expanduser())
        folder = load_api_key("HINDSIGHT_OFFSITE_FOLDER", required=False) or DEFAULT_FOLDER
        drive = drive_service(interactive=False)
        scratch = data_write_dir("runtime:scratch/tmp")
    except failures as exc:
        jobs = {label: exc for label, _ in KINDS}   # both kinds failed at setup; both are reported
    else:
        jobs = {"snapshot": lambda: run(drive, base, scratch, passphrase, folder, now),
                "machine-config": lambda: run_machine_config(
                    drive, data_path("machine-config-manifest"), scratch, passphrase, folder, now, ROOT, [ROOT, get_data_root()])}
    return run_all(jobs, base, notify_own_sinks, failures)


def cmd_authorize(_args) -> int:
    from scripts.utils.clone_guard import require_main_clone

    require_main_clone(__file__)
    folder = load_api_key("HINDSIGHT_OFFSITE_FOLDER", required=False) or DEFAULT_FOLDER
    try:
        drive = drive_service(interactive=True)
        print(f"token written: {TOKEN} (scope drive.file)")
        print(f"Drive folder {folder!r}: {ensure_folder(drive, folder)}")
    except _failures() as exc:
        # One line, not a traceback: MEASURED 2026-10-07 the first consent ended in a
        # page of stack for a browser timeout and for "Drive API disabled in the project".
        print(f"authorize failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_status(args) -> int:
    base = data_write_dir("runtime:hindsight-snapshot/cache")
    states = {label: load_state(base, state_file) for label, state_file in KINDS}
    print(json.dumps({label: s or {"state": "no run recorded"} for label, s in states.items()},
                     ensure_ascii=False, indent=1))
    if args.remote:
        from scripts.utils.gmail_auth import ConsentRequired

        folder = load_api_key("HINDSIGHT_OFFSITE_FOLDER", required=False) or DEFAULT_FOLDER
        try:
            drive = drive_service(interactive=False)
            found = drive.find_folders(folder)
        except (ConsentRequired, *_failures()) as exc:
            print(f"remote status failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        if len(found) != 1:
            print(f"{len(found)} folders named {folder!r} carry our mark")
            return 1
        for f in sorted(drive.list(found[0]["id"]), key=lambda f: f["name"]):
            mine = ours(f, found[0]["id"]) or ours(f, found[0]["id"], MC_NAME_RE)
            mark = "ours" if mine else "NOT OURS, never touched"
            print(f"  {f['name']}  {f.get('size')} bytes  {(f.get('appProperties') or {}).get('state')}  [{mark}]")
    return 0 if all(s.get("state") == "ok" for s in states.values()) else 1


# ============================================================
# Self-test: a fake Drive in memory and an invented snapshot; nothing leaves the machine
# ============================================================


class FakeDrive:
    """The `GoogleDrive` surface over a dict. `corrupt_download` flips one byte on the way back."""

    def __init__(self):
        self.files: dict[str, dict] = {}
        self.blobs: dict[str, bytes] = {}
        self.trashed: list[str] = []
        self.corrupt_download = False
        self.n = 0

    def _id(self) -> str:
        self.n += 1
        return f"fake-{self.n}"

    def find_folders(self, name):
        return [f for f in self.files.values() if f.get("mimeType") == FOLDER_MIME and f["name"] == name
                and (f.get("appProperties") or {}).get(MARK_KEY) == MARK_VALUE]

    def create_folder(self, name):
        i = self._id()
        self.files[i] = {"id": i, "name": name, "mimeType": FOLDER_MIME, "appProperties": {MARK_KEY: MARK_VALUE}}
        return i

    def add(self, folder, name, props, data=b"x"):
        i = self._id()
        self.files[i] = {"id": i, "name": name, "parents": [folder], "appProperties": dict(props or {}),
                         "size": str(len(data)), "md5Checksum": hashlib.md5(data, usedforsecurity=False).hexdigest()}
        self.blobs[i] = data
        return self.files[i]

    def list(self, folder_id):
        return [dict(f) for f in self.files.values() if folder_id in (f.get("parents") or [])]

    def upload(self, folder_id, path, name, props):
        return dict(self.add(folder_id, name, props, Path(path).read_bytes()))

    def download(self, file_id, dest):
        data = self.blobs[file_id]
        if self.corrupt_download:
            data = data[:100] + bytes([data[100] ^ 1]) + data[101:]
        Path(dest).write_bytes(data)

    def update(self, file_id, name, props):
        self.files[file_id].update(name=name, appProperties=dict(props))
        return dict(self.files[file_id])

    def trash(self, file_id):
        self.trashed.append(file_id)
        self.files.pop(file_id)
        self.blobs.pop(file_id)


MARKER = "an-invented-observation-marker-7f3c"


def _invented_snapshot(base: Path) -> None:
    snap = snapshot_module()
    out = {}
    for i in range(40):
        out.update(snap.observation_files({"id": f"obs-{i:03d}", "text": f"{MARKER} number {i}\n" * 50,
                                           "mentioned_at": "2026-01-02T00:00:00Z"}))
    out.update(snap.directive_files({"id": "dir-1", "name": "Invented rule", "content": "be invented"}))
    out.update(snap.bundle_files([{"path": "index.md", "content": "# invented index"}]))
    counts = {"observations": 40, "documents": 0, "mental-models": 0, "directives": 1, "knowledge-base-files": 1}
    manifest = {"format": snap.FORMAT, "bank": "inv", "taken_at": "2026-01-02T04:10:00+00:00", "counts": counts,
                "documents_listed": 0, "documents_mirrored_from_files": 0, "files": len(out),
                "bytes": sum(len(b) for b in out.values()), "sha256": snap.tree_digest(out)}
    base.mkdir(parents=True, exist_ok=True)
    snap.write_tree(base, out, manifest)


def _passphrase(tmp: Path, mode: int = 0o600, text: str = "an invented passphrase for the self-test only") -> Path:
    p = tmp / "passphrase"
    p.write_text(text + "\n", encoding="utf-8")
    p.chmod(mode)
    return p


def _setup(tmp: Path) -> tuple[Path, Path, Path]:
    base, work = tmp / "snap", tmp / "work"
    work.mkdir()
    _invented_snapshot(base)
    return base, work, _passphrase(tmp)


T0 = datetime(2026, 1, 2, 4, 40, tzinfo=timezone.utc)


def _st_a_run_uploads_only_ciphertext_and_accepts_it_after_restore_check(tmp: Path) -> None:
    base, work, pp = _setup(tmp)
    drive = FakeDrive()
    rec = run(drive, base, work, pp, "inv-folder", T0)
    folder = drive.find_folders("inv-folder")[0]["id"]
    files = drive.list(folder)
    assert [f["name"] for f in files] == ["hindsight-snapshot-20260102T044000Z.tar.zst.gpg"], files
    assert files[0]["appProperties"]["state"] == "verified" and rec["sha256"] == files[0]["appProperties"]["sha256"]
    blob = drive.blobs[files[0]["id"]]
    assert MARKER.encode() not in blob and not blob.startswith(b"\x28\xb5\x2f\xfd"), "plaintext reached Drive"
    assert not list(work.iterdir()), "scratch was not cleaned"
    enc, dec = tmp / "c.gpg", tmp / "c.tar.zst"
    enc.write_bytes(blob)
    decrypt(enc, dec, pp)
    ok, report = restore_check(dec, work)
    assert ok, report
    other = tmp / "other"
    other.mkdir()
    try:
        decrypt(enc, tmp / "wrong.tar.zst", _passphrase(other, text="a different invented passphrase"))
    except OffsiteError:
        pass
    else:
        raise AssertionError("a copy decrypted under the wrong passphrase")


def _st_a_damaged_copy_is_never_accepted_and_the_last_good_one_stands(tmp: Path) -> None:
    base, work, pp = _setup(tmp)
    drive = FakeDrive()
    good = run(drive, base, work, pp, "inv-folder", T0)
    drive.corrupt_download = True
    try:
        run(drive, base, work, pp, "inv-folder", T0.replace(day=3))
    except OffsiteError as exc:
        assert "differs" in str(exc), exc
    else:
        raise AssertionError("a copy that came back damaged was accepted")
    names = [f["name"] for f in drive.list(drive.find_folders("inv-folder")[0]["id"])]
    assert names == [good["name"]], names
    assert len(drive.trashed) == 1 and not list(work.iterdir())


def _st_restore_check_failure_refuses_the_copy(tmp: Path) -> None:
    base, work, pp = _setup(tmp)
    m = json.loads((base / "current" / "manifest.json").read_text(encoding="utf-8"))
    m["sha256"] = "0" * 64
    (base / "current" / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    drive = FakeDrive()
    try:
        run(drive, base, work, pp, "inv-folder", T0)
    except OffsiteError as exc:
        assert "restore-check refused" in str(exc), exc
    else:
        raise AssertionError("a copy restore-check refuses was accepted")
    assert drive.list(drive.find_folders("inv-folder")[0]["id"]) == [] and len(drive.trashed) == 1


def _st_no_snapshot_and_a_loose_passphrase_file_are_refused(tmp: Path) -> None:
    (tmp / "work").mkdir()
    (tmp / "empty").mkdir()
    for bad in (_passphrase(tmp, 0o644), tmp / "missing"):
        try:
            check_passphrase_file(bad)
        except OffsiteError:
            continue
        raise AssertionError(f"{bad.name}: an unsafe passphrase file was accepted")
    try:
        run(FakeDrive(), tmp / "empty", tmp / "work", _passphrase(tmp), "inv-folder", T0)
    except OffsiteError as exc:
        assert "no whole snapshot" in str(exc), exc
    else:
        raise AssertionError("a run without a snapshot uploaded something")


def _st_retention_keeps_7_daily_and_4_weekly_and_touches_only_ours(tmp: Path) -> None:
    drive = FakeDrive()
    folder = drive.create_folder("inv-folder")
    mark = {MARK_KEY: MARK_VALUE, "state": "verified"}
    assert to_prune(drive.list(folder), folder) == [], "pruned with no verified copy"
    start = date(2026, 1, 1)
    for d in range(60):   # 2026-01-01 .. 2026-03-01, which is a Sunday
        day = start.toordinal() + d
        stamp = date.fromordinal(day).strftime("%Y%m%d") + "T044000Z"
        drive.add(folder, f"hindsight-snapshot-{stamp}.tar.zst.gpg", mark)
    drive.add(folder, "hindsight-snapshot-20260210T050000Z.tar.zst.gpg.pending", {**mark, "state": "pending"})
    drive.add(folder, "hindsight-snapshot-20260301T050000Z.tar.zst.gpg.pending", {**mark, "state": "pending"})
    drive.add(folder, "hindsight-snapshot-20260101T000000Z.tar.zst.gpg", {})              # no mark
    drive.add(folder, "a file of the operator's.pdf", {MARK_KEY: MARK_VALUE})             # not our name
    drive.add("elsewhere", "hindsight-snapshot-20260101T000001Z.tar.zst.gpg", mark)       # not our folder
    for f in to_prune(drive.list(folder), folder):
        drive.trash(f["id"])
    left = sorted(f["name"] for f in drive.list(folder))
    days = ["20260208", "20260215", "20260222"] + [f"202602{d}" for d in range(23, 29)] + ["20260301"]
    want = sorted([f"hindsight-snapshot-{d}T044000Z.tar.zst.gpg" for d in days]
                  + ["hindsight-snapshot-20260301T050000Z.tar.zst.gpg.pending",
                     "hindsight-snapshot-20260101T000000Z.tar.zst.gpg", "a file of the operator's.pdf"])
    assert left == want, left
    assert len(drive.list("elsewhere")) == 1


def _st_a_failure_notifies_once_until_a_run_succeeds(tmp: Path) -> None:
    sent = []
    for _ in range(2):
        alarm(tmp, "an invented failure", lambda text: sent.append(text) or True)
    assert len(sent) == 1 and "FAILED" in sent[0], sent
    save_state(tmp, {"state": "ok"})
    alarm(tmp, "another invented failure", lambda text: sent.append(text) or True)
    assert len(sent) == 2 and oct((tmp / STATE_FILE).stat().st_mode & 0o777) == "0o600"


def _st_the_real_drive_calls_say_what_the_fake_assumes(tmp: Path) -> None:
    """`GoogleDrive` through the Google client library and its bundled discovery document, over
    `HttpMockSequence`: the mock is the only transport, so no request can leave the machine."""
    from googleapiclient.discovery import build
    from googleapiclient.http import HttpMockSequence

    from scripts.utils.google_user_agent import _force
    from scripts.utils.user_agent import user_agent

    class Recording(HttpMockSequence):
        """Forces the workspace User-Agent the way `google_http` does, and records each request."""

        def __init__(self, seq):
            super().__init__(seq)
            self.seen = []

        def request(self, uri, method="GET", body=None, headers=None, *a, **kw):
            headers = _force(headers)
            self.seen.append((method, uri, body, headers.get("user-agent")))
            return super().request(uri, method, body, headers, *a, **kw)

    http = Recording([
        ({"status": "200"}, json.dumps({"files": [{"id": "a"}], "nextPageToken": "t2"})),
        ({"status": "200"}, json.dumps({"files": [{"id": "b"}]})),
        ({"status": "200", "location": "https://upload.invalid/session"}, ""),
        ({"status": "200"}, json.dumps({"id": "up", "name": "n", "size": "5"})),
        ({"status": "200", "content-range": "bytes 0-4/5"}, b"hello"),
        ({"status": "200"}, json.dumps({"id": "up"})),
    ])
    drive = GoogleDrive(build("drive", "v3", http=http))
    assert [f["id"] for f in drive.list("F1")] == ["a", "b"], "a second page was not read"
    (tmp / "x").write_bytes(b"hello")
    assert drive.upload("F1", tmp / "x", "n", {MARK_KEY: MARK_VALUE})["id"] == "up"
    drive.download("up", tmp / "y")
    assert (tmp / "y").read_bytes() == b"hello"
    drive.trash("up")
    assert {ua for *_, ua in http.seen} == {user_agent()}, http.seen
    (m1, u1, _, _), (_, u2, _, _), (m3, u3, b3, _), _, (_, u5, _, _), (m6, u6, b6, _) = http.seen
    q1 = urllib.parse.parse_qs(urllib.parse.urlsplit(u1).query)
    assert m1 == "GET" and q1["q"] == ["'F1' in parents and trashed=false"], u1
    assert "pageToken=t2" in u2, u2
    assert m3 == "POST" and "uploadType=resumable" in u3, u3
    assert json.loads(b3) == {"name": "n", "parents": ["F1"], "appProperties": {MARK_KEY: MARK_VALUE}}, b3
    assert "/files/up" in u5 and "alt=media" in u5, u5
    assert m6 == "PATCH" and "/files/up" in u6 and json.loads(b6) == {"trashed": True}, (m6, u6, b6)


MC_MARKER = "an-invented-key-line-91d2"


def _mc_setup(tmp: Path) -> tuple[Path, Path, Path, Path]:
    """An invented machine: a home with a Claude file, a skills dir with a `.bak` and a cache,
    a systemd dir with a `.wants` link, an ssh dir with an invented key, and an engine repository
    with an invented `.env` and one tracked file. Returns (manifest, engine, work, passphrase)."""
    home, engine, work = tmp / "home", tmp / "engine", tmp / "work"
    for d in (home / "claude/skills/a/__pycache__", home / "systemd/default.target.wants", home / "ssh",
              engine / "conf", work):
        d.mkdir(parents=True)
    (home / "claude/CLAUDE.md").write_text("# invented user instructions\n", encoding="utf-8")
    (home / "claude/skills/a/SKILL.md").write_text("invented skill\n", encoding="utf-8")
    (home / "claude/skills/a/SKILL.md.bak-1").write_text("old\n", encoding="utf-8")
    (home / "claude/skills/a/__pycache__/x.pyc").write_bytes(b"\0")
    (home / "systemd/inv.service").write_text("[Service]\nExecStart=/bin/true\n", encoding="utf-8")
    (home / "systemd/default.target.wants/inv.service").symlink_to("../inv.service")
    (home / "ssh/id_invented").write_text(f"{MC_MARKER} key\n", encoding="utf-8")
    (home / "ssh/id_invented").chmod(0o600)
    (home / "ssh/id_invented.pub").write_text("ssh-ed25519 AAAAinvented\n", encoding="utf-8")
    (engine / ".env").write_text(f"INVENTED_TOKEN={MC_MARKER}\n", encoding="utf-8")
    (engine / "conf/tracked.txt").write_text("tracked\n", encoding="utf-8")
    (engine / "conf/local.txt").write_text("local\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(engine)], check=True)
    subprocess.run(["git", "-C", str(engine), "add", "conf/tracked.txt"], check=True)

    def entry(i, source, kind, secret=False):
        return {"id": i, "source": source, "kind": kind, "secret": secret, "what": f"invented {i}",
                "restore_to": source, "restore_steps": [f"invented step for {i}"]}

    doc = {"exclude": ["__pycache__", "*.pyc"], "passphrase": "Kept in an invented place.",
           "before": ["Clone the invented repositories."], "after": ["Reload the invented units."],
           "entries": [entry("claude-md", str(home / "claude/CLAUDE.md"), "file"),
                       entry("skills", str(home / "claude/skills"), "dir"),
                       entry("systemd", str(home / "systemd"), "dir"),
                       entry("ssh", str(home / "ssh"), "dir", True),
                       entry("env", "{engine}/.env", "file", True),
                       entry("conf", "{engine}/conf", "dir"),
                       entry("not-here", str(home / "absent"), "file")]}
    manifest = tmp / "manifest.yaml"
    manifest.write_text(json.dumps(doc), encoding="utf-8")   # JSON is YAML; the loader is the real one
    return manifest, engine, work, _passphrase(tmp)


def _members(blob: bytes, tmp: Path, pp: Path) -> dict[str, bytes]:
    enc, dec = tmp / "mc.gpg", tmp / "mc.tar.zst"
    enc.write_bytes(blob)
    decrypt(enc, dec, pp)
    raw = subprocess.run(["zstd", "-q", "-d", "-c", str(dec)], capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tf:
        return {m.name: tf.extractfile(m).read() for m in tf if m.isfile()}


def _st_machine_config_is_verified_with_a_readme_beside_every_entry_and_no_secret_in_one(tmp: Path) -> None:
    manifest, engine, work, pp = _mc_setup(tmp)
    drive = FakeDrive()
    folder = drive.create_folder("inv-folder")
    mark = {MARK_KEY: MARK_VALUE, "state": "verified"}
    old = [f"machine-config-202511{d:02d}T044000Z.tar.zst.gpg" for d in range(1, 21)]
    for name in [*old, "hindsight-snapshot-20251101T044000Z.tar.zst.gpg"]:
        drive.add(folder, name, mark)
    rec = run_machine_config(drive, manifest, work, pp, "inv-folder", T0, engine, [engine])
    assert rec["pruned"] and set(rec["pruned"]) <= set(old), rec["pruned"]
    left = {f["name"] for f in drive.list(folder)}
    assert "hindsight-snapshot-20251101T044000Z.tar.zst.gpg" in left, "machine-config pruned a snapshot copy"
    files = [f for f in drive.list(folder) if f["name"].startswith("machine-config-2026")]
    assert [f["name"] for f in files] == ["machine-config-20260102T044000Z.tar.zst.gpg"], files
    assert files[0]["appProperties"]["state"] == "verified" and not list(work.iterdir())
    blob = drive.blobs[files[0]["id"]]
    assert MC_MARKER.encode() not in blob and not blob.startswith(b"\x28\xb5\x2f\xfd"), "plaintext reached Drive"
    assert rec["missing"] == ["not-here"] and rec["entries"] == 6 and rec["secret_files"] == 3, rec
    assert MC_MARKER not in json.dumps(rec), "a secret reached the run's record"
    m = _members(blob, tmp, pp)
    r = f"{MC_ROOT}/"
    assert m[r + "env/.env"] == (engine / ".env").read_bytes()
    assert m[r + "ssh/ssh/id_invented"] == (tmp / "home/ssh/id_invented").read_bytes()
    for base in ("claude-md/CLAUDE.md", "skills/skills", "systemd/systemd", "ssh/ssh", "env/.env", "conf/conf"):
        assert r + base + ".README.md" in m, f"no README beside {base}"
    assert not any("not-here" in n for n in m), "a README for a source that is not there"
    docs = {n: b.decode() for n, b in m.items() if n.endswith(".md") and not n.endswith("SKILL.md")}
    assert all(MC_MARKER not in text for text in docs.values()), "a secret value reached a README"
    restore = docs[r + "RESTORE.md"]
    assert all(f"`{e}`" in restore for e in ("claude-md", "skills", "systemd", "ssh", "env", "conf", "not-here"))
    assert "NOT PRESENT" in restore and "Kept in an invented place." in restore
    assert not any(n.endswith((".bak-1", ".pyc")) for n in m), "an excluded file was archived"
    assert r + "conf/conf/local.txt" in m and r + "conf/conf/tracked.txt" not in m, "tracked file archived"
    assert "`tracked.txt`: tracked in a repository" in docs[r + "conf/conf.README.md"]
    assert not any("default.target.wants" in n for n in m), "a symlink was archived"
    sysreadme = docs[r + "systemd/systemd.README.md"]
    assert "`systemctl --user enable inv.service`" in sysreadme and "-> `../inv.service`" in sysreadme
    assert "YES" in docs[r + "env/.env.README.md"] and hashlib.sha256(m[r + "env/.env"]).hexdigest() in docs[
        r + "env/.env.README.md"]


def _st_machine_config_verification_refuses_a_copy_unlike_its_source(tmp: Path) -> None:
    manifest, engine, work, pp = _mc_setup(tmp)
    doc = load_machine_manifest(manifest)
    recs = [collect_entry(e, engine, set(), []) for e in doc["entries"]]
    plain = work / "a.tar.zst"
    expected = build_machine_archive(recs, doc, plain, "20260102T044000Z", "t", "h", pp)
    verify_machine_archive(plain, expected, recs)   # the honest case passes
    secret_member = f"{MC_ROOT}/env/.env"
    for bad, word in (({**expected, secret_member: "0" * 64}, "1 differ"),
                      ({**expected, f"{MC_ROOT}/invented": "0" * 64}, "1 missing"),
                      ({k: v for k, v in expected.items() if k != secret_member}, "1 extra")):
        try:
            verify_machine_archive(plain, bad, recs)
        except OffsiteError as exc:
            assert word in str(exc) and MC_MARKER not in str(exc), exc
        else:
            raise AssertionError(f"a copy with {word} member was accepted")
    ghost = {"entry": {**doc["entries"][0], "id": "ghost"}, "src": tmp / "ghost", "present": False,
             "files": [], "links": [], "skipped": []}
    try:
        verify_machine_archive(plain, expected, [*recs, ghost])
    except OffsiteError as exc:
        assert "RESTORE.md does not name entry ghost" in str(exc), exc
    else:
        raise AssertionError("an entry missing from RESTORE.md was accepted")
    for broken in ({"entries": []}, {"entries": [{**doc["entries"][0], "kind": "socket"}]},
                   {"entries": [doc["entries"][0], doc["entries"][0]]}):
        (tmp / "bad.yaml").write_text(json.dumps(broken), encoding="utf-8")
        try:
            load_machine_manifest(tmp / "bad.yaml")
        except OffsiteError:
            continue
        raise AssertionError(f"a malformed manifest was accepted: {broken}")


def _st_one_kind_failing_never_skips_or_hides_the_other_and_retention_stays_per_kind(tmp: Path) -> None:
    manifest, engine, work, pp = _mc_setup(tmp)
    (tmp / "empty").mkdir()
    drive, sent = FakeDrive(), []
    jobs = {"snapshot": lambda: run(drive, tmp / "empty", work, pp, "inv-folder", T0),
            "machine-config": lambda: run_machine_config(drive, manifest, work, pp, "inv-folder", T0, engine,
                                                         [engine])}
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        rc = run_all(jobs, tmp, lambda text: sent.append(text) or True, (OffsiteError,))
    assert rc == 1 and len(sent) == 1 and "snapshot copy FAILED" in sent[0], sent
    assert load_state(tmp)["state"] == "failing" and load_state(tmp, MC_STATE_FILE)["state"] == "ok"
    assert "machine-config copy verified" in out.getvalue() and MC_MARKER not in out.getvalue()
    assert MC_MARKER not in (tmp / MC_STATE_FILE).read_text(encoding="utf-8")
    sent.clear()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        rc = run_all({label: OffsiteError("invented setup failure") for label, _ in KINDS}, tmp,
                     lambda text: sent.append(text) or True, (OffsiteError,))
    assert rc == 1 and len(sent) == 1 and "machine-config copy FAILED" in sent[0], sent   # snapshot already failing
    folder = drive.create_folder("retention")
    mark = {MARK_KEY: MARK_VALUE, "state": "verified"}
    for d in range(1, 29):
        drive.add(folder, f"hindsight-snapshot-202602{d:02d}T044000Z.tar.zst.gpg", mark)
        drive.add(folder, f"machine-config-202601{d:02d}T044000Z.tar.zst.gpg", mark)
    snap_prune = to_prune(drive.list(folder), folder)
    mc_prune = to_prune(drive.list(folder), folder, MC_NAME_RE)
    assert snap_prune and all(f["name"].startswith("hindsight-snapshot-") for f in snap_prune)
    assert mc_prune and all(f["name"].startswith("machine-config-") for f in mc_prune)
    # 28 daily copies each: 7 days kept, plus the newest of 2 older ISO weeks; 19 go, per kind
    assert len(snap_prune) == len(mc_prune) == 19, (len(snap_prune), len(mc_prune))


SELF_TESTS = [
    _st_the_real_drive_calls_say_what_the_fake_assumes,
    _st_a_run_uploads_only_ciphertext_and_accepts_it_after_restore_check,
    _st_a_damaged_copy_is_never_accepted_and_the_last_good_one_stands,
    _st_restore_check_failure_refuses_the_copy,
    _st_no_snapshot_and_a_loose_passphrase_file_are_refused,
    _st_retention_keeps_7_daily_and_4_weekly_and_touches_only_ours,
    _st_a_failure_notifies_once_until_a_run_succeeds,
    _st_machine_config_is_verified_with_a_readme_beside_every_entry_and_no_secret_in_one,
    _st_machine_config_verification_refuses_a_copy_unlike_its_source,
    _st_one_kind_failing_never_skips_or_hides_the_other_and_retention_stays_per_kind,
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
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[1],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("authorize", help="once, by hand: browser consent for the drive.file token").set_defaults(
        fn=cmd_authorize)
    sub.add_parser("run", help="archive, encrypt, upload, verify, prune").set_defaults(fn=cmd_run)
    s = sub.add_parser("status", help="the last verified copy, from local state")
    s.add_argument("--remote", action="store_true", help="also list the Drive folder (reads only)")
    s.set_defaults(fn=cmd_status)
    sub.add_parser("self-test", help="fake Drive, invented snapshot; nothing leaves the machine").set_defaults(
        fn=cmd_self_test)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
