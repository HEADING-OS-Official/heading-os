#!/usr/bin/env python3
"""Pull service-host state from the managed VM into a read-only laptop mirror.

The service-host VM is a separate entity this workspace MANAGES: once its
daemons go live the VM is the authoritative writer of their state. This
laptop-side client copies that state (via scp over SSH) into the mirror dir
on the laptop so the laptop has a current read-only view. The laptop never
writes back.

The VM host address is read from SERVICE_VM_HOST in .env (a secret, never in
the engine). The VM repo path, mirror dir, and state-dir layout come from the
private config/service-host.json (engine ships scripts/service-host.example.json).
Some state dirs may not exist on the VM yet (e.g., before a daemon's first
run); those report as "not present" - that's not a failure.

Usage:
    python scripts/pull-service-state.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.workspace import get_data_root, load_env
from scripts.utils.rmtree import rmtree_force
from scripts.utils.data_path import DataPathError, data_path
from scripts.utils.service_host import _data_rel, _vm_path, service_config, state_dirs, vm_roots
from scripts.utils.colors import GREEN, YELLOW, RED, CYAN, GRAY, BOLD, RESET

# scp over a slow or half-open link hung the run indefinitely; this is a
# per-directory ceiling, not a whole-run one.
SCP_TIMEOUT_S = 600


def mirror_dir() -> Path:
    """The mirror directory: the registry name `service-mirror`. Call time.

    The `mirror_dir` key of `service-host.json` is no longer read (HOS-5
    section 7, 2026-09-25): its default pointed into a tree the HEADING OS 2
    move had emptied, so pulls landed where nothing reads. A config that still
    carries the key is told so on every run rather than silently obeyed,
    because obeying it is exactly how the old address came back.
    """
    svc, _ = service_config()
    if "mirror_dir" in svc:
        print(f"{YELLOW}service-host.json: `mirror_dir` is ignored since "
              f"2026-09-25; the mirror is the registry name `service-mirror`. "
              f"Remove the key.{RESET}")
    return data_path("service-mirror")




def main() -> int:
    # load_env FIRST. `get_data_root()` reads HEADING_OS_DATA out of os.environ,
    # and `load_env()` is what copies .env into os.environ, so resolving the
    # root above the load read the override before it existed. MEASURED
    # 2026-08-30 with HEADING_OS_DATA in .env: the root came back as the
    # examples fallback before the load and as the operator's overlay after it,
    # so every run wrote the mirror under the wrong root and reported success.
    # This is the same ordering bug `vm_roots` above already burned this file
    # once, in the same run, for the same reason.
    load_env()
    # The mirror is PRIVATE data, so it must resolve under the DATA root,
    # never the engine clone.
    data_root = get_data_root()
    try:
        targets = state_dirs()
    except ValueError as exc:
        print(f"{RED}service-host.json: {exc}{RESET}")
        return 1
    host = os.environ.get("SERVICE_VM_HOST")
    if not host:
        print(f"{RED}SERVICE_VM_HOST not set in .env{RESET}")
        print(f"{GRAY}Add a line:  SERVICE_VM_HOST=<vm-ip-or-hostname>{RESET}")
        return 1

    # Bound once, not re-read per entry: `mirror` below and `dest_rel` inside
    # the loop must name the same directory, or the staging swap writes one
    # tree and renames another.
    try:
        mirror = mirror_dir()
    except DataPathError as exc:
        print(f"{RED}mirror directory: {exc}{RESET}")
        return 1
    mirror_rel_dir = mirror.relative_to(data_root).as_posix()
    mirror.mkdir(parents=True, exist_ok=True)
    print(f"{BOLD}Pulling service-host state from {host}{RESET}")

    pulled = skipped = failures = 0
    for name, vm_path in targets:
        dest_abs = mirror / name
        # Pull into a SCRATCH directory and swap on success. The old code
        # rmtree'd the live mirror first, so an unreachable VM or a half-finished
        # transfer left the operator with neither the new copy nor the last good
        # one -- and this mirror is the only local record of the VM's state.
        staging_abs = mirror / f".{name}.incoming"
        if staging_abs.exists():
            rmtree_force(staging_abs)
        # Relative dest (resolved against cwd=data_root below) avoids the
        # Windows-drive-letter colon issue in scp.
        dest_rel = f"{mirror_rel_dir}/.{name}.incoming"
        cmd = ["scp", "-r", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
               f"root@{host}:{vm_path}", dest_rel]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    cwd=data_root, timeout=SCP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            rmtree_force(staging_abs)
            print(f"  {YELLOW}{name}: scp exceeded {SCP_TIMEOUT_S}s; "
                  f"previous mirror left intact{RESET}")
            failures += 1
            continue
        if result.returncode == 0:
            if dest_abs.exists():
                rmtree_force(dest_abs)
            staging_abs.rename(dest_abs)
            print(f"  {GREEN}{name}{RESET}")
            pulled += 1
        else:
            rmtree_force(staging_abs)
            err = (result.stderr.strip().splitlines() or ["unknown error"])[-1]
            stderr_lower = result.stderr.lower()
            if "no such file" in stderr_lower or "not a regular file" in stderr_lower:
                print(f"  {GRAY}{name}: not present on VM yet{RESET}")
                skipped += 1
            else:
                print(f"  {YELLOW}{name}: {err}{RESET}")
                failures += 1

    summary = f"pulled={pulled} skipped={skipped} failed={failures}"
    if failures:
        print(f"{YELLOW}{summary}{RESET}")
        return 1
    print(f"{GREEN}{summary}{RESET}")
    print(f"{GRAY}mirror: {mirror}{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
