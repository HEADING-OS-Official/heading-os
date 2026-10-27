#!/usr/bin/env python3
"""Write ONE Lific dump to a tracked path, so a copy of the tracker leaves this machine.

The problem this solves, measured 2026-09-20. The overlay ignores every path
Lific writes: the `*.db` under the `runtime:lific` store, its `-wal` and `-shm` sidecars, and the
`backups/` directory Lific rotates hourly on `retain = 24`. That is the
instruction in `LIFIC.md` section 3 (now under `spec-docs` in the data
repository), confirmed by the operator the same
day, and it is right for what it addresses: a binary that changes on every write
entered history whole on every backup, and eight commits had already carried 24
rotating archives of data the database beside them already held.

It leaves one thing open, and this script is that thing. With all four paths
ignored, nothing about the issue tracker reached the remote at all. A dump
written to a path the overlay tracks restores an off-machine copy at the cost of
ONE archive per backup rather than a fresh database blob per write.

Why the destination sits inside the runtime store it is surrounded by. The
operator decided on 2026-09-19, asked directly, that the Lific database stays in
the `runtime:lific` store, and `scripts/runtime-services.example.yaml` records
that decision with an instruction not to "fix" it. Putting the dump anywhere else
would invent a new top-level convention for one file. So it lands beside the
store as the single TRACKED file there, and the overlay's `.gitignore` carries
the same note where a reader looking at those rules will find it.

`lific dump` is safe against a live instance: the database is snapshotted through
SQLite's online backup and no writer lock is held. The archive it writes is mode
0600 and carries the database, every attachment blob and a `manifest.json`.

HELM only. The dump reads a store that a running service writes, which is the
category `CLAUDE.md` puts in the main clone, and `scripts/push-all.py` is a HELM
path already.

Usage:
  python scripts/lific-backup.py                 # write the dump, report the path
  python scripts/lific-backup.py --out PATH      # somewhere else
  python scripts/lific-backup.py --quiet         # one line on success

Exit codes:
  0  the dump was written, OR `lific` is not installed on this machine.
  1  `lific` is installed and the dump failed. Any previous dump is left alone.
  2  refused: not the main clone, or no data overlay.
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.clone_guard import require_main_clone
from scripts.utils.colors import BOLD, GRAY, GREEN, RED, RESET, YELLOW
from scripts.utils.paths import DataRootError
from scripts.utils.data_path import DataPathError, data_write_dir

# The live instance is pinned by this config, per `LIFIC.md` section 2.2 (under
# `spec-docs` in the data repository).
# An env override exists so a second instance can be dumped without editing the
# script; absent, the documented location is used.
CONFIG_ENV = "HEADING_OS_LIFIC_CONFIG"
DEFAULT_CONFIG = Path.home() / ".config" / "lific" / "lific.toml"

# `lific dump` on a database of this size took under a second when measured on
# 2026-09-20. The bound is here so a hung binary cannot stall a backup run.
DUMP_TIMEOUT = 300


def destination(data_root: Path | None = None) -> Path:
    """The one tracked archive. Overwritten in place, never accumulated.

    `data_write_dir("runtime:lific/state")` and not `runtime_root`, and the difference is a kind
    segment rather than a style. MEASURED 2026-09-20: the first version wrote
    to `runtime_root()/lific/lific-backup.tar.gz`, and the runtime write
    guard's `misrouted()` rule refused it, because an address under the runtime
    store carries `<service>/<kind>/` and this one carried no kind at all. The
    rule was right: an address nobody can route is an address the registry
    cannot describe, and this store already holds seven services that do carry
    one. What drove that rule over this tree went with the 2026-09-21
    test-suite split, so the kind segment below rests on review now.

    `state` is the kind, because this archive is neither a log nor a cache nor
    a temporary: it is the only copy of the tracker that leaves this machine,
    and nothing rebuilds it from disk.

    It raises `DataPathError` rather than resolve inside the engine clone, so a
    public clone with `lific` installed never writes a dump into the engine.
    """
    return (data_write_dir("runtime:lific/state", data_root=data_root)
            / "lific-backup.tar.gz")


def config_path() -> Path:
    override = os.environ.get(CONFIG_ENV, "").strip()
    return Path(override).expanduser() if override else DEFAULT_CONFIG


def write_dump(out: Path, *, config: Path, binary: str) -> None:
    """Run `lific dump` into `out`, atomically. Raises RuntimeError on failure.

    The dump goes to a sibling temporary file and is moved into place with
    `os.replace`, so a failed or half-written run never destroys the archive
    already there. That matters more here than in most places: the file being
    replaced may be the only copy of the tracker that exists off this machine.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    # The suffix ends in `.tmp` rather than beginning with it, because the
    # overlay's ignore rule for `*.tmp` under the runtime store matches a SUFFIX. Written as
    # `.tmp-<pid>` until 2026-09-20, which no rule matched, so an interrupted
    # dump left an untracked wreck beside the tracked archive with nothing to
    # ignore it. The pid stays: two runs must not stage over each other.
    staged = out.with_name(f"{out.name}.{os.getpid()}.tmp")
    try:
        proc = subprocess.run(
            [binary, "dump", "--config", str(config), "--out", str(staged)],
            capture_output=True, text=True, timeout=DUMP_TIMEOUT, check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"`lific dump` exited {proc.returncode}: "
                f"{(proc.stderr or proc.stdout or '').strip() or 'no output'}")
        if not staged.is_file() or staged.stat().st_size == 0:
            raise RuntimeError(
                f"`lific dump` exited 0 and wrote no archive at {staged}")
        os.replace(staged, out)
        out.chmod(0o600)
    finally:
        if staged.exists():
            staged.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=None,
                        help="destination archive (default: the tracked path "
                             "beside the Lific store)")
    parser.add_argument("--config", type=Path, default=None,
                        help=f"lific.toml (default: ${CONFIG_ENV}, else "
                             f"~/.config/lific/lific.toml)")
    parser.add_argument("--quiet", action="store_true",
                        help="one line on success")
    args = parser.parse_args()

    require_main_clone(__file__)

    binary = shutil.which("lific")
    if binary is None:
        if not args.quiet:
            print(f"{GRAY}lific is not installed here; no tracker to back "
                  f"up.{RESET}")
        return 0

    try:
        out = args.out if args.out is not None else destination()
    except (DataRootError, DataPathError) as exc:  # a public clone has nowhere to write one; the front door says so as DataPathError
        print(f"{RED}no writable data overlay, so there is nowhere to put the "
              f"dump: {exc}{RESET}", file=sys.stderr)
        return 2

    config = args.config if args.config is not None else config_path()
    if not config.is_file():
        print(f"{RED}lific is installed but its config is not at {config}. "
              f"Set ${CONFIG_ENV} or pass --config.{RESET}", file=sys.stderr)
        return 1

    try:
        write_dump(out, config=config, binary=binary)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"{RED}the Lific dump failed, so no new copy of the tracker was "
              f"written: {exc}{RESET}", file=sys.stderr)
        if out.is_file():
            print(f"{YELLOW}the previous dump is still in place at {out}, and "
                  f"it is now stale.{RESET}", file=sys.stderr)
        return 1

    size_kb = out.stat().st_size / 1024
    if args.quiet:
        print(f"{GREEN}lific dump -> {out} ({size_kb:.0f} KB){RESET}")
    else:
        print(f"{BOLD}{GREEN}Lific dump written{RESET}")
        print(f"  {out}")
        print(f"  {size_kb:.0f} KB, mode 0600, one file, overwritten each run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
