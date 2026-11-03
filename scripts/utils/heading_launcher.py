#!/usr/bin/env python3
"""Put the `heading` command on PATH: the launcher step of the installer.

Deployment design § 2 step 10 (registry name `plans`): a `heading` command
that opens Claude in the engine folder, so the executive never `cd`s into it.

The installer's step `launcher` (scripts/heading-install.py, HOS-312) checks
with `launcher_ready` and runs `install_launcher`, relaying
`LauncherResult.message`. It is idempotent: a rerun rewrites the same bytes and
adds no second profile line. The yard refusal stays in `main` below, so the
step itself runs in a scratch HOME too.

What it writes, and nothing else:
  * `<home>/.local/bin/heading`, a two-line wrapper that execs
    `<engine>/scripts/heading.sh` by absolute path (no symlink, M044.1),
    written atomically at mode 755;
  * that directory on PATH for every new terminal, through
    `scripts/utils/shell_path.add_to_path`, the one helper the installer uses
    too (the login file the user's shell reads and its interactive rc).

The CLI refuses from a YARD unless `--home` names a home other than the real
one: the real command must point at the main clone, because a yard's checkout
is deleted after its merge.

Usage (by hand, or from the installer):
    .venv/bin/python scripts/utils/heading_launcher.py            # this engine, this user
    .venv/bin/python scripts/utils/heading_launcher.py --home DIR # a scratch home
"""
from __future__ import annotations

import argparse
import shlex
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.utils.atomic import atomic_write_text  # noqa: E402
from scripts.utils.shell_path import add_to_path, is_real_home, on_path  # noqa: E402


@dataclass
class LauncherResult:
    command: Path
    profiles: list[Path]
    message: str


def _wrapper(engine_root: Path) -> str:
    target = engine_root.resolve() / "scripts" / "heading.sh"
    return f"#!/usr/bin/env bash\nexec bash {shlex.quote(str(target))} \"$@\"\n"


def launcher_ready(engine_root: Path, *, home: Path | None = None) -> bool:
    """True when <home>/.local/bin/heading is a regular file with exactly the
    wrapper's bytes for this engine, at mode 755."""
    command = (home or Path.home()).resolve() / ".local" / "bin" / "heading"
    try:
        st = command.lstat()
        return (stat.S_ISREG(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o755
                and command.read_bytes() == _wrapper(engine_root).encode("utf-8"))
    except FileNotFoundError:
        return False  # not installed yet: the step writes it
    except OSError as exc:
        print(f"  note: {command} could not be read ({exc}); writing it again")
        return False


def install_launcher(engine_root: Path, *, home: Path | None = None,
                     path_env: str | None = None,
                     login: str | None = None) -> LauncherResult:
    home = (home or Path.home()).resolve()
    if not (engine_root / "scripts" / "heading.sh").is_file():
        raise FileNotFoundError(f"{engine_root}/scripts/heading.sh is absent")
    bin_dir = home / ".local" / "bin"
    command = bin_dir / "heading"
    atomic_write_text(command, _wrapper(engine_root), mode=0o755)
    changed = add_to_path(bin_dir, home=home, login=login)
    if on_path(bin_dir, path_env):
        return LauncherResult(command, changed, f"`heading` is ready: {command}")
    return LauncherResult(command, changed,
                          f"`heading` is installed at {command}; open a new terminal "
                          f"so it is on PATH")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--home", type=Path, help="install for this home directory (verification)")
    args = ap.parse_args(argv)
    if args.home is None or is_real_home(args.home):
        # The real ~/.local/bin must point at the main clone: a yard's checkout
        # is deleted after its merge and would leave a dead command behind.
        from scripts.utils.clone_guard import require_main_clone
        require_main_clone(__file__)
    engine = Path(__file__).resolve().parents[2]
    print(install_launcher(engine, home=args.home).message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
