#!/usr/bin/env python3
"""Shared rules for the four Hindsight updaters (HOS-359).

The update manager applies `hindsight-server`, `hindsight-control-plane`,
`hindsight-cli` and `hindsight-coding-agents` through the scripts beside this
module. What they share lives here, once:

- versions: read through the registry (`resolve_current`, `latest_version`),
  each checked against `^\\d+(\\.\\d+)*$` before it reaches a command line;
- the exit contract: 0 = applied, already current, or "not now"; 1 = failed or
  refused, with one line naming the state left behind; 2 is never used;
- one lock, `hindsight-update.lock` under the registry name
  `update-manager-locks`, taken non-blocking (busy is "not now", exit 0);
- backup names `<original name>.bak-<YYYYMMDD-HHMMSS>-<old version>`;
- the operator notification on a failure after a change was attempted, to his
  own sink only (`telegram_notify.notify` over `own_targets()`);
- `ssh` with list arguments, `BatchMode=yes` and a per-call timeout, and a
  dry-run mode that prints every command instead of running it.

How the manager drives a script apply (read 2026-11-13 in
`scripts/utils/update_apply.py`): exit 0 is recorded as applied and `current`
is re-read, so a "not now" exit 0 leaves the component pending for the next
run; any non-zero exit is recorded as rolled back and counts toward the
circuit breaker. A script apply skips the registry `health` block, so each
script is its own health gate.

Usage: imported by the four `scripts/updaters/hindsight_*_update.py` scripts.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import math
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

ENGINE_ROOT = Path(__file__).resolve().parent.parent.parent
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from scripts.updaters.cliproxyapi_update import (  # noqa: E402
    _normalise_version,
    _version_tuple,
)
from scripts.utils import update_sources  # noqa: E402
from scripts.utils.api import load_api_key  # noqa: E402
from scripts.utils.data_path import DataPathError, data_write_dir  # noqa: E402
from scripts.utils.telegram_notify import notify, own_targets  # noqa: E402
from scripts.utils.update_common import resolve_current  # noqa: E402
from scripts.utils.update_registry import Component, load_registry  # noqa: E402

REGISTRY = ENGINE_ROOT / "config" / "update-registry.yaml"
HOST_SETTING = "HINDSIGHT_ADMIN_SSH_HOST"
LOCK_NAME = "hindsight-update.lock"
VERSION_RE = re.compile(r"^\d+(\.\d+)*$")
SSH_CONNECT_TIMEOUT_S = 20

# The time budget. The manager kills a script apply at the registry's
# `apply.timeout_s`, default 900 (`update_apply._default_applier`, read
# 2026-11-13), and then records "rolled-back" with no rollback at all. A kill
# after a point of no return (a stopped service, a swapped unit or binary)
# would therefore leave the component half-changed with nobody told. So each
# script measures from its own start (this import, the earliest line it runs)
# and, right before each point of no return, refuses with "not now" (exit 0)
# unless the worst case of everything after it fits, plus a margin.
STARTED = time.monotonic()
DEFAULT_TIMEOUT_S = 900
MARGIN_S = 120
# One notification send per own sink: requests' connect and read timeouts of
# 30 s each (`TelegramBot._call`, read 2026-11-13), so 60 s per sink.
NOTIFY_PER_SINK_S = 60


class Outcome(Exception):
    """The end of a run: an exit code, the one line printed, and an optional
    notification text sent to the operator's own sink when the code is 1."""

    def __init__(self, code: int, line: str, notify_text: str | None = None):
        super().__init__(line)
        self.code = code
        self.line = line
        self.notify_text = notify_text


# ============================================================
# Arguments, versions, comparison
# ============================================================

def base_parser(description: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--dry-run", action="store_true",
                   help="print the commands in order; no network, ssh, lock or file change")
    p.add_argument("--current", help="the installed version to assume (with --dry-run)")
    p.add_argument("--latest", help="the target version to assume (with --dry-run)")
    return p


def check_dry_args(args: argparse.Namespace) -> None:
    if args.dry_run and not (args.current and args.latest):
        raise Outcome(1, "--dry-run needs --current and --latest; nothing changed")
    if not args.dry_run and (args.current or args.latest):
        raise Outcome(1, "--current and --latest are for --dry-run only; nothing changed")


def safe_version(value: str, what: str) -> str:
    """`value` when it is bare dotted digits, else refuse: every version ends up
    inside a shell string on the server or in a file name."""
    if not isinstance(value, str) or not VERSION_RE.match(value):
        raise Outcome(1, f"refusing {what} {value!r}: not a dotted version; nothing changed")
    return value


def component(name: str) -> Component:
    for comp in load_registry(REGISTRY):
        if comp.name == name:
            return comp
    raise Outcome(1, f"component {name} is not in the registry; nothing changed")


def read_current(comp: Component) -> str:
    """The installed version of `comp`, normalised, or refuse."""
    raw = resolve_current(comp)
    current = _normalise_version(raw)
    if not current:
        raise Outcome(1, f"{comp.name}: the current version was not answered; nothing changed")
    return safe_version(current, f"{comp.name} current version")


def read_versions(comp: Component, args: argparse.Namespace) -> tuple[str, str]:
    """(current, latest) for `comp`: from the arguments in a dry run, else from
    the registry probes. Refuses an empty answer and a source error."""
    if args.dry_run:
        return (safe_version(args.current, "--current"),
                safe_version(args.latest, "--latest"))
    current = read_current(comp)
    try:
        raw_latest = update_sources.latest_version(comp.latest, current)
    except update_sources.SourceError as exc:
        raise Outcome(1, f"{comp.name}: the latest version was not answered ({exc}); "
                         "nothing changed") from exc
    latest = _normalise_version(raw_latest)
    if not latest:
        raise Outcome(1, f"{comp.name}: the latest version was not answered; nothing changed")
    return current, safe_version(latest, f"{comp.name} latest version")


def compare(name: str, current: str, latest: str) -> None:
    """Raise the outcome for equal (exit 0) or a downgrade (exit 1); return when
    `latest` is newer."""
    if _version_tuple(current) == _version_tuple(latest):
        raise Outcome(0, f"{name}: already current at {current}")
    if _version_tuple(latest) < _version_tuple(current):
        raise Outcome(1, f"{name}: refusing a downgrade: installed {current}, "
                         f"'latest' is {latest}; nothing changed")


def server_on(target: str, dry: bool) -> None:
    """The order rule of the console and the CLI: they follow the server.

    Read with the `hindsight-server` registry probe (the laptop CLI through the
    tunnel). Not on the target yet: exit 0, nothing changed, so the component
    stays pending and the next run retries."""
    if dry:
        print(f"# read the server version with resolve_current(hindsight-server); "
              f"assumed {target} in this dry run")
        return
    raw = resolve_current(component("hindsight-server"))
    server = _normalise_version(raw)
    if not server or _version_tuple(server) != _version_tuple(target):
        shown = server or "not answered"
        raise Outcome(0, f"server not on {target} yet (it reports {shown}); "
                         "waits; nothing changed")


# ============================================================
# Lock, names, notification
# ============================================================

def take_lock() -> IO[str]:
    """The shared Hindsight update lock, held until the process exits. Busy:
    exit 0, "not now". Never `.apply.lock`, which the manager itself holds."""
    try:
        path = data_write_dir("update-manager-locks") / LOCK_NAME
        fh = path.open("a", encoding="utf-8")
    except (DataPathError, OSError) as exc:
        raise Outcome(1, f"cannot open the Hindsight update lock ({exc}); nothing changed") from exc
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise Outcome(0, "another Hindsight update is running; not now") from None
    return fh


def stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def backup_name(original: str, ts: str, old_version: str) -> str:
    return f"{original}.bak-{ts}-{old_version}"


def admin_host(dry: bool) -> str:
    """The ssh alias with root on the memory server, from `.env`. Unset: refuse,
    nothing touched. A value starting with '-' would be read by ssh as an
    option, so it is refused too."""
    if dry:
        return f"<{HOST_SETTING}>"
    host = (load_api_key(HOST_SETTING, required=False) or "").strip()
    if not host:
        raise Outcome(1, f"{HOST_SETTING} is not set in .env; nothing changed")
    if host.startswith("-") or any(ch.isspace() for ch in host):
        raise Outcome(1, f"{HOST_SETTING} is not a plain ssh alias; nothing changed")
    return host


def notify_worst_s() -> int:
    """The longest the failure notification can take: one send per own sink."""
    return NOTIFY_PER_SINK_S * max(1, len(own_targets()))


@dataclass
class Budget:
    """What is left of the manager's timeout for this script."""

    timeout_s: int
    dry: bool

    @classmethod
    def of(cls, comp: Component, dry: bool) -> Budget:
        raw = (comp.apply or {}).get("timeout_s", DEFAULT_TIMEOUT_S)
        try:
            timeout_s = int(raw)
        except (TypeError, ValueError) as exc:
            raise Outcome(1, f"{comp.name}: apply.timeout_s {raw!r} is not a number; "
                             "nothing changed") from exc
        return cls(timeout_s, dry)

    def left_s(self) -> float:
        return self.timeout_s - (time.monotonic() - STARTED) - MARGIN_S

    def require(self, need_s: int, step: str, terms: str) -> None:
        """Exit 0 "not now" unless `need_s` more seconds fit before the kill.

        Called right before a point of no return; `terms` names the sum."""
        left = self.left_s()
        if self.dry:
            print(f"# budget before {step}: needs up to {need_s} s ({terms}); "
                  f"timeout_s {self.timeout_s}, margin {MARGIN_S} s")
            return
        if need_s > left:
            raise Outcome(0, f"{step} not now: the rest needs up to {need_s} s ({terms}), "
                             f"{max(0, int(left))} s left of timeout_s {self.timeout_s} "
                             f"after a {MARGIN_S} s margin; nothing changed past this point")


def wait_until(check, wait_s: int, poll_s: int, probe_cap_s: int, dry: bool) -> bool:
    """Poll `check(timeout)` until it answers True or `wait_s` has passed.

    Each probe's timeout is capped by the time left in the window, and so is
    each sleep, so the loop ends within `wait_s + 1` seconds (the cap rounds
    up to whole seconds) rather than overshooting by a whole probe."""
    deadline = time.monotonic() + wait_s
    while True:
        left = deadline - time.monotonic()
        if check(max(1, min(probe_cap_s, math.ceil(left)))):
            return True
        left = deadline - time.monotonic()
        if dry or left <= 0:
            return False
        time.sleep(min(poll_s, left))


def keep_copy_cmd(path: str, backup: str) -> str:
    """The remote command that keeps `path` as `backup`, and fails unless the
    copy is new and byte-identical. `cp -n` exits 0 when it skips an existing
    target on some coreutils releases, so it cannot prove the copy was made."""
    p, b = shlex.quote(path), shlex.quote(backup)
    return f"test ! -e {b} && cp -p {p} {b} && cmp -s {p} {b}"


def send_notification(text: str) -> None:
    delivered = sum(1 for t in sorted(own_targets()) if notify(t, text))
    if not delivered:
        print("NO NOTIFICATION SENT: no operator sink is configured or the transport refused",
              file=sys.stderr)


def sri_sha512(hex_digest: str) -> str:
    """`sha512-<base64>`, the npm `dist.integrity` form, from a hex sha512."""
    return "sha512-" + base64.b64encode(bytes.fromhex(hex_digest)).decode("ascii")


def file_sha512_sri(path: Path) -> str:
    h = hashlib.sha512()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return sri_sha512(h.hexdigest())


def npm_integrity(package: str, version: str, dry: bool) -> str:
    """`dist.integrity` of `package@version` from the npm registry, or refuse."""
    if dry:
        return "<integrity from npm registry>"
    if not re.match(r"^@[\w.-]+/[\w.-]+$", package):
        raise Outcome(1, f"refusing npm package name {package!r}; nothing changed")
    url = f"https://registry.npmjs.org/{package}/{version}"
    try:
        body = json.loads(update_sources.fetch_text(url))
    except (update_sources.SourceError, json.JSONDecodeError) as exc:
        raise Outcome(1, f"npm registry did not answer for {package}@{version} ({exc}); "
                         "nothing changed") from exc
    dist = body.get("dist") if isinstance(body, dict) else None
    integrity = dist.get("integrity") if isinstance(dist, dict) else None
    if not isinstance(integrity, str) or not integrity.startswith("sha512-"):
        raise Outcome(1, f"npm registry gave no sha512 integrity for {package}@{version}; "
                         "nothing changed")
    return integrity


def github_json(url: str) -> dict:
    try:
        body = json.loads(update_sources.fetch_text(url))
    except (update_sources.SourceError, json.JSONDecodeError) as exc:
        raise Outcome(1, f"GitHub API did not answer ({exc}); nothing changed") from exc
    if not isinstance(body, dict):
        raise Outcome(1, f"GitHub API answered {type(body).__name__} for {url}; nothing changed")
    return body


def safe_repo(repo: str) -> str:
    if not isinstance(repo, str) or not re.match(r"^[\w.-]+/[\w.-]+$", repo):
        raise Outcome(1, f"refusing repository name {repo!r}; nothing changed")
    return repo


# ============================================================
# Running commands
# ============================================================

@dataclass
class Result:
    rc: int
    out: str = ""
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.rc == 0

    def why(self) -> str:
        tail = (self.err or self.out).strip()[-300:]
        return f"exit {self.rc}" + (f": {tail}" if tail else "")


@dataclass
class Runner:
    """Runs local commands and remote ones over ssh, or prints them in a dry
    run. `dry_out` is the output a dry run pretends to get, so one code path
    serves both modes. Never `shell=True`: a remote command is one argument to
    ssh, and every value inside it went through `shlex.quote` at the caller."""

    dry: bool
    host: str = ""
    log: list[str] = field(default_factory=list)

    def local(self, argv: list[str], timeout: int, dry_out: str = "",
              env: dict | None = None, cwd: Path | None = None) -> Result:
        shown = shlex.join(argv)
        if self.dry:
            print(("(cd " + shlex.quote(str(cwd)) + ") " if cwd else "") + shown)
            return Result(0, dry_out)
        try:
            res = subprocess.run(argv, capture_output=True, text=True, errors="replace",
                                 timeout=timeout, check=False, env=env,
                                 cwd=str(cwd) if cwd else None)
        except subprocess.TimeoutExpired:
            return Result(124, "", f"timed out after {timeout} s: {shown}")
        except OSError as exc:
            return Result(127, "", f"{type(exc).__name__}: {exc}")
        return Result(res.returncode, res.stdout, res.stderr)

    def remote(self, cmd: str, timeout: int, dry_out: str = "") -> Result:
        argv = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT_S}",
                "--", self.host, cmd]
        if self.dry:
            print(shlex.join(argv))
            return Result(0, dry_out)
        return self.local(argv, timeout)

    def note(self, text: str) -> None:
        """A step that is not a command (an HTTP read, a comparison)."""
        if self.dry:
            print(f"# {text}")


def remote_exists(run: Runner, path: str, timeout: int = 60,
                  dry_present: bool = False) -> bool:
    """True when `path` exists on the server; refuse when the check itself
    failed. `dry_present` is the answer a dry run assumes."""
    q = shlex.quote(path)
    res = run.remote(f"if test -e {q}; then echo present; else echo absent; fi",
                     timeout, dry_out="present" if dry_present else "absent")
    word = res.out.strip()
    if not res.ok or word not in ("present", "absent"):
        raise Outcome(1, f"could not check {path} on the server ({res.why()}); nothing changed")
    return word == "present"


def sed_path_swap(old_dir: str, new_dir: str, unit: str) -> str:
    """The remote `sed -i` that repoints `unit` from one version directory to
    the next. Both directories are built from checked versions, so they hold
    only `/`, letters, digits, `-` and dots; the dots are escaped."""
    pattern = old_dir.replace(".", r"\.")
    return f"sed -i {shlex.quote(f's#{pattern}#{new_dir}#g')} {shlex.quote(unit)}"


def finish(run_fn, name: str, dry: bool = False) -> int:
    """Run `run_fn()`, turn its `Outcome` into the exit code, and notify on a
    failure that carries a notification text."""
    try:
        run_fn()
    except Outcome as out:
        print(("[dry run, nothing ran] " if dry else "") + out.line)
        if out.code != 0 and out.notify_text and not dry:
            send_notification(out.notify_text)
        return out.code
    print(f"{name}: finished without an outcome; treated as failed")
    return 1
