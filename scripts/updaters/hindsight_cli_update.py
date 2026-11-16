#!/usr/bin/env python3
"""Apply script for the `hindsight-cli` update-manager component, the laptop's
Hindsight CLI in ~/.local/bin (HOS-359). No ssh.

Follows the server: it applies only when the server, read through the
`hindsight-server` registry probe with the CLI still installed, already reports
the target version; otherwise it waits with exit 0. Then: download the release
asset `hindsight-linux-amd64` over https, compare its sha256 with the asset's
`digest` in the GitHub release API, keep the running binary under its backup
name `hindsight.bak-<YYYYMMDD-HHMMSS>-<old version>`, install with mode 755 by
an atomic rename, and check that `hindsight --version` prints the target and
`hindsight version -o json` answers with `api_version`. A failure puts the kept
binary back, exits 1 and notifies.

Exit codes: 0 applied, already current, or not now; 1 failed or refused.

Usage:
  .venv/bin/python scripts/updaters/hindsight_cli_update.py
  .venv/bin/python scripts/updaters/hindsight_cli_update.py --dry-run --current 1.2.1 --latest 1.2.2
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from scripts.updaters import hindsight_common as hc  # noqa: E402
from scripts.utils.data_path import DataPathError, data_write_dir  # noqa: E402
from scripts.utils.user_agent import user_agent  # noqa: E402

NAME = "hindsight-cli"
ASSET = "hindsight-linux-amd64"
BIN = Path.home() / ".local" / "bin" / "hindsight"
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
T_CHECK = 60
# The worst case from the install to the exit: the install (a local copy and a
# rename) 10, `--version` 60, `version -o json` 60, putting the kept binary
# back 10 = 140, plus the notification. The registry gives this script the
# manager's default timeout of 900 s; `Budget.require` checks this sum with the
# real elapsed time right before the backup copy and answers "not now" when it
# does not fit. Before that the installed binary is untouched.
POST_INSTALL_TERMS = (("install", 10), ("--version", T_CHECK),
                      ("version -o json", T_CHECK), ("put back", 10))
POST_INSTALL_S = sum(s for _, s in POST_INSTALL_TERMS)


def _asset(repo: str, tag: str) -> tuple[str, str]:
    """(download URL, sha256 hex) of the asset in release `tag`, or refuse."""
    release = hc.github_json(f"https://api.github.com/repos/{repo}/releases/tags/{tag}")
    for asset in release.get("assets") or []:
        if isinstance(asset, dict) and asset.get("name") == ASSET:
            url, digest = asset.get("browser_download_url"), asset.get("digest")
            if not isinstance(url, str) or not url.startswith("https://"):
                raise hc.Outcome(1, f"{ASSET} has no https download URL; nothing changed")
            m = re.fullmatch(r"sha256:([0-9a-f]{64})", digest or "")
            if not m:
                raise hc.Outcome(1, f"{ASSET} in {tag} carries no sha256 digest; "
                                    "refusing an unverified install; nothing changed")
            return url, m.group(1)
    raise hc.Outcome(1, f"release {tag} has no asset {ASSET}; nothing changed")


def _download(url: str, dest: Path) -> str:
    """Download `url` (https only, capped) to `dest`; return its sha256 hex."""
    if not url.startswith("https://"):
        raise ValueError(f"refusing a non-https download URL: {url!r}")
    req = urllib.request.Request(url, headers={"User-Agent": user_agent()})  # noqa: S310 - scheme checked above
    h, written = hashlib.sha256(), 0
    with urllib.request.urlopen(req, timeout=120) as resp, dest.open("wb") as fh:  # noqa: S310 - scheme checked above
        while chunk := resp.read(1 << 20):
            written += len(chunk)
            if written > MAX_DOWNLOAD_BYTES:
                raise ValueError(f"download exceeded {MAX_DOWNLOAD_BYTES} bytes: {url!r}")
            h.update(chunk)
            fh.write(chunk)
    return h.hexdigest()


def _healthy(run: hc.Runner, latest: str) -> bool:
    ver = run.local([str(BIN), "--version"], T_CHECK, dry_out=f"hindsight {latest}")
    m = re.search(r"hindsight (\S+)", ver.out)
    if not (ver.ok and m and hc._normalise_version(m.group(1)) == latest):
        return False
    api = run.local([str(BIN), "version", "-o", "json"], T_CHECK,
                    dry_out=f'{{"api_version": "{latest}"}}')
    return api.ok and re.search(r'"api_version"\s*:\s*"[^"]+"', api.out) is not None


def _install(src: Path) -> None:
    """Stage beside the target, mode 755, then an atomic rename over it."""
    side = BIN.with_name(BIN.name + ".incoming")
    shutil.copy2(src, side)
    side.chmod(0o755)
    os.replace(side, BIN)


def run_update(args) -> None:
    hc.check_dry_args(args)
    run = hc.Runner(dry=args.dry_run)
    if not args.dry_run:
        _lock = hc.take_lock()  # noqa: F841 - held until the process exits
    comp = hc.component(NAME)
    budget = hc.Budget.of(comp, args.dry_run)
    current, latest = hc.read_versions(comp, args)
    hc.compare(NAME, current, latest)
    hc.server_on(latest, run.dry)
    repo = hc.safe_repo(comp.latest.get("repo", ""))
    backup = BIN.with_name(hc.backup_name(BIN.name, hc.stamp(), current))
    vers = f"{current} -> {latest}"

    if run.dry:
        run.note(f"GET https://api.github.com/repos/{repo}/releases/tags/v{latest}: "
                 f"asset {ASSET}, its browser_download_url and digest")
        run.note(f"download <browser_download_url> over https; sha256 == "
                 "<digest from GitHub release>")
        budget.require(POST_INSTALL_S + hc.notify_worst_s(), "the install",
                       " + ".join(f"{n} {v}" for n, v in POST_INSTALL_TERMS)
                       + f" + notification {hc.notify_worst_s()}")
        print(f"cp -p {BIN} {backup}")
        print(f"install -m 755 <download> {BIN}  (staged as {BIN.name}.incoming, os.replace)")
        _healthy(run, latest)
        raise hc.Outcome(0, f"{NAME}: applied {vers}")

    if backup.exists():
        raise hc.Outcome(1, f"{backup} already exists; nothing changed")
    if not BIN.is_file():
        raise hc.Outcome(1, f"{BIN} is not there to back up; nothing changed")
    url, want = _asset(repo, f"v{latest}")
    try:
        stage_root = data_write_dir("runtime:scratch/tmp")
    except DataPathError as exc:
        raise hc.Outcome(1, f"no scratch directory ({exc}); nothing changed") from exc
    with tempfile.TemporaryDirectory(prefix="hindsight-cli.", dir=stage_root) as td:
        newbin = Path(td) / ASSET
        try:
            got = _download(url, newbin)
        except (OSError, ValueError) as exc:
            raise hc.Outcome(1, f"download of {ASSET} failed ({exc}); nothing changed") from exc
        if got != want:
            raise hc.Outcome(1, f"sha256 mismatch for {ASSET} ({got} != {want}); "
                                "refusing; nothing changed",
                             f"Hindsight CLI update {vers}: the downloaded {ASSET} did not "
                             "match its published sha256; nothing installed")
        budget.require(POST_INSTALL_S + hc.notify_worst_s(), "the install",
                       " + ".join(f"{n} {v}" for n, v in POST_INSTALL_TERMS)
                       + f" + notification {hc.notify_worst_s()}")
        try:
            shutil.copy2(BIN, backup)
        except OSError as exc:
            raise hc.Outcome(1, f"cannot keep {BIN} as {backup} ({exc}); nothing changed") from exc
        try:
            _install(newbin)
            ok = _healthy(run, latest)
        except OSError as exc:
            print(f"install failed ({type(exc).__name__}: {exc})")
            ok = False
    if ok:
        raise hc.Outcome(0, f"{NAME}: applied {vers}; old binary kept as {backup}")
    try:
        _install(backup)
        state = f"{current} put back from {backup}"
    except OSError as exc:
        state = f"PUTTING {current} BACK FAILED ({exc}); {BIN} state unknown"
    line = f"{latest} failed its health check; {state}"
    raise hc.Outcome(1, line, f"Hindsight CLI update {vers}: {line}")


def main() -> int:
    args = hc.base_parser(__doc__.splitlines()[0]).parse_args()
    return hc.finish(lambda: run_update(args), NAME, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
