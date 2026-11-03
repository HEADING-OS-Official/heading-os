#!/usr/bin/env python3
"""The Hindsight web console, installed and started by `scripts/heading-install.py` (HOS-312).

Usage (from the installer, never by hand):
    from scripts.utils import install_hindsight_console as console
    console.check_install(ctx); console.run_install(ctx)   # and check_service, run_service

Two steps after the Hindsight server. What they leave, relative to the
executive's home:

    ~/.local/share/heading-os/hindsight-control-plane/1.1.0/   the npm package, unpacked
    ~/.config/heading-os/hindsight/console.env                 its two keys, data plane and login (install_hindsight.run_settings)
    hindsight-control-plane.service                            the service layer's family of that name

The package is `@vectorize-io/hindsight-control-plane`, version VERSION, the
same as the server's (install_hindsight.VERSION). Facts read 2026-11-02 from the
unpacked 1.1.0 tarball, never by running it:
- a prebuilt Next.js standalone build: `standalone/server.js` and its
  `node_modules` are in the tarball, so no npm install and no install script runs;
- `bin/cli.js` takes `--hostname`, `--port`, `--api-url` (lines 34 to 40), binds
  0.0.0.0 by default (line 31), and starts `standalone/server.js` with the `node`
  it finds on PATH (line 93), so the unit puts the installer's Node first on PATH;
- Next.js 16.3.8 needs Node 20.9 or newer (standalone/node_modules/next/package.json,
  "engines"), which the installer's Node floor of 22 covers;
- the server sends the key from HINDSIGHT_CP_DATAPLANE_API_KEY to the API;
- with no HINDSIGHT_CP_ACCESS_KEY set, its middleware lets every page and every
  /api/ route through without a login;
- `/` redirects to `/dashboard` (the `[locale]/page` module calls `redirect`),
  so the root answers a 3xx.

Every function takes its home, its `systemctl` and its Python from the step
context, as install_hindsight.py does. Linux and macOS, as the server is (on a
Mac the agent reads console.env through scripts/utils/env_exec.py, HOS-341;
UNTESTED on a Mac); where hindsight-tunnel is installed, the steps refuse with
the server's own sentence and the checks are false.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import tarfile
import tempfile
import time
from pathlib import Path

from scripts.utils import install_hindsight as hs
from scripts.utils.install_runner import changed_since_start, service_pid
from scripts.utils.web_interfaces import HINDSIGHT_CONSOLE_URL, answers

VERSION = "1.1.0"
PACKAGE = "@vectorize-io/hindsight-control-plane"
TARBALL = ("https://registry.npmjs.org/@vectorize-io/hindsight-control-plane/-/"
           f"hindsight-control-plane-{VERSION}.tgz")
# `npm view @vectorize-io/hindsight-control-plane@1.1.0 dist.integrity` gave
# sha512-z4yMWTfwrgXG3DMwoRRgoBIXSHrcX0V/xXGZeDELRFPNPZkyHmGecCGxyDONIZNXqB/FplypwwOwOF06FkaJaA==
# and the downloaded tarball matched it; its SHA-256 is pinned here (MEASURED 2026-11-02).
SHA256 = "8ef170c524e7cef6210ae15ea0fefd09cbd4f7ec8959a16d1b30eef109652a70"  # pragma: allowlist secret (a published checksum)
START_SCRIPT = Path("bin") / "cli.js"
FAMILY = "hindsight-control-plane"
UNIT = f"{FAMILY}.service"
LAUNCHD_LABEL = f"com.headingos.{FAMILY}"  # schedule.LAUNCHD_PREFIX + the service stem
READY_WAIT_S = 60


def base_dir(home: Path) -> Path:
    return home / ".local" / "share" / "heading-os" / "hindsight-control-plane"


def install_dir(home: Path) -> Path:
    """The unpacked package (placeholder HINDSIGHT_CONSOLE_DIR of the service layer)."""
    return base_dir(home) / VERSION


def _refusal(ctx) -> str | None:
    """The server's own reason not to run here, or None."""
    return hs.tunnel_conflict(ctx)


# ============================================================
# Step hindsight-console: the pinned package, unpacked
# ============================================================

def check_install(ctx) -> bool:
    if _refusal(ctx):
        return False
    folder = install_dir(hs._home(ctx))
    try:
        version = json.loads((folder / "package.json").read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError, AttributeError):
        return False
    return version == VERSION and (folder / START_SCRIPT).is_file()


def run_install(ctx) -> None:
    refusal = _refusal(ctx)
    if refusal:
        raise hs.HindsightError(refusal)
    from scripts.utils.install_index import InstallError, download  # https only, verified against SHA256

    base = base_dir(hs._home(ctx))
    base.mkdir(parents=True, exist_ok=True)
    target = install_dir(hs._home(ctx))
    with tempfile.TemporaryDirectory(dir=base) as tmp:
        archive = Path(tmp) / f"hindsight-control-plane-{VERSION}.tgz"
        print(f"  downloading the Hindsight web console {VERSION}")
        try:
            download(TARBALL, archive, SHA256)
        except InstallError as exc:
            raise hs.HindsightError(str(exc)) from exc
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(Path(tmp) / "x", filter="data")
        unpacked = Path(tmp) / "x" / "package"  # npm tarballs hold one folder, package/
        if not (unpacked / START_SCRIPT).is_file():
            raise hs.HindsightError(f"the {PACKAGE} {VERSION} archive holds no {START_SCRIPT}; nothing was installed")
        if target.exists():
            shutil.rmtree(target)
        os.replace(unpacked, target)
    for other in base.iterdir():  # exactly one version installed
        if other.is_dir() and other.name != VERSION:
            shutil.rmtree(other)


# ============================================================
# Step hindsight-console-service: the console in the background
# ============================================================

def _pid(ctx) -> int | None:
    return service_pid(hs._systemctl(ctx), UNIT, LAUNCHD_LABEL, hs._env(ctx))


def check_service(ctx) -> bool:
    if getattr(ctx, "dry_run", False) or _refusal(ctx):
        return False  # its check asks the console, and a dry run sends nothing
    if not ctx.python.exists():
        return False
    return (hs._services(ctx, "status", FAMILY).returncode == 0 and answers(HINDSIGHT_CONSOLE_URL)
            and not changed_since_start(hs.console_env_path(hs._home(ctx)), _pid(ctx)))


def run_service(ctx) -> None:
    refusal = _refusal(ctx)
    if refusal:
        raise hs.HindsightError(refusal)
    if hs._services(ctx, "install", FAMILY, capture=False).returncode != 0:
        raise hs.HindsightError(f"the service layer could not install {FAMILY}; see the lines above "
                                f"(scripts/heading-services.py install {FAMILY})")
    if changed_since_start(hs.console_env_path(hs._home(ctx)), _pid(ctx)):
        from scripts.utils.schedule import systemd_user_dir, written_by_service_layer

        if platform.system() == "Darwin":
            unit = hs._home(ctx) / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        else:
            unit = systemd_user_dir(hs._env(ctx)) / UNIT
        if unit.exists() and not written_by_service_layer(unit):
            print(f"  {unit} was not written by HEADING OS and is left untouched, so it is not restarted. "
                  f"To load the new key, run yourself: {hs.restart_hint(UNIT, LAUNCHD_LABEL)}")
        else:
            proc = hs._run(ctx, hs.restart_command(ctx, UNIT, LAUNCHD_LABEL), timeout=120)
            if proc.returncode != 0:
                raise hs.HindsightError(f"the Hindsight web console could not be restarted: {hs._why(proc)}")
    deadline = time.monotonic() + READY_WAIT_S
    while not answers(HINDSIGHT_CONSOLE_URL):
        if time.monotonic() >= deadline:
            raise hs.HindsightError(f"{UNIT} runs but {HINDSIGHT_CONSOLE_URL}/ does not answer; "
                                    f"read its log: {hs.log_hint(UNIT, LAUNCHD_LABEL)}")
        time.sleep(2)
