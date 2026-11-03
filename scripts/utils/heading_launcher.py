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

The installer's handoff (HOS-336) also runs the setup wizard inside Herdr
through `open_in_herdr`, in its own process or as the detached helper
`--herdr-workspace`; that mode writes no file and asks Herdr for one workspace.

Usage (by hand, or from the installer):
    .venv/bin/python scripts/utils/heading_launcher.py            # this engine, this user
    .venv/bin/python scripts/utils/heading_launcher.py --home DIR # a scratch home
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import stat
import subprocess
import sys
import time
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


# ============================================================
# The setup wizard inside Herdr (HOS-336)
# ============================================================
# Read from Herdr 0.9.5's own CLI (`herdr --help`, `herdr workspace create
# --help`, `herdr pane run --help`, MEASURED 2026-11-03): Herdr has no option
# that starts it with a workspace and a first command. Bare `herdr` starts or
# attaches the interface; `herdr workspace create --cwd <dir> --label <text>
# --focus` and `herdr pane run <pane id> <command>` talk to a running server
# through its socket, and `workspace create` answers with
# `.result.root_pane.pane_id` (`herdr --skill`; read the same way by
# scripts/herdr/heading-os-yard/yard-bootstrap.sh). `pane run` types the line
# into the pane's shell and exits 0 once it is dispatched, not once the
# program started (scripts/utils/yard_sessions.py).

HERDR_LABEL = "HEADING OS"
HERDR_WAIT_S = 60


class HerdrHandoffError(RuntimeError):
    """The workspace could not be made or the wizard not typed into it; the message says which."""


def in_herdr(env: dict[str, str]) -> bool:
    """True when this process runs inside a Herdr pane, where a second Herdr must never start."""
    return bool(env.get("HERDR_PANE_ID")) or env.get("HERDR_ENV") == "1"


def wizard_line(heading: Path) -> str:
    """The shell line the Herdr pane runs: the `heading` command by absolute path."""
    return f"{shlex.quote(str(heading))} /setup-wizard"


def workspace_argv(herdr: Path, engine: Path) -> list[str]:
    return [str(herdr), "workspace", "create", "--cwd", str(engine), "--label", HERDR_LABEL, "--focus"]


def open_in_herdr(herdr: Path, engine: Path, heading: Path, *, env: dict[str, str],
                  wait_s: float = 0) -> str:
    """Create the HEADING OS workspace in the running Herdr and start the wizard in it; its pane id.

    `wait_s` > 0: first wait up to that long for a Herdr server to answer,
    for a Herdr that a new window is starting at this moment.
    """
    def herdr_run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(herdr), *args], env=env, capture_output=True, text=True,
                              errors="replace", timeout=15)

    deadline = time.monotonic() + wait_s
    while wait_s > 0:
        probe = herdr_run("workspace", "list")
        if probe.returncode == 0:
            break
        if time.monotonic() >= deadline:
            raise HerdrHandoffError(f"no Herdr server answered within {wait_s:g} s: "
                                    f"{(probe.stdout + probe.stderr).strip()[-200:]}")
        time.sleep(1)
    made = subprocess.run(workspace_argv(herdr, engine), env=env, capture_output=True, text=True,
                          errors="replace", timeout=15)
    if made.returncode != 0:
        raise HerdrHandoffError(f"herdr workspace create exited {made.returncode}: "
                                f"{(made.stdout + made.stderr).strip()[-200:]}")
    try:
        pane = json.loads(made.stdout)["result"]["root_pane"]["pane_id"]
    except (ValueError, KeyError, TypeError) as exc:
        raise HerdrHandoffError(f"herdr workspace create gave no root pane ({type(exc).__name__})") from exc
    ran = herdr_run("pane", "run", str(pane), wizard_line(heading))
    if ran.returncode != 0:
        raise HerdrHandoffError(f"herdr pane run exited {ran.returncode}: {(ran.stdout + ran.stderr).strip()[-200:]}")
    return str(pane)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--home", type=Path, help="install for this home directory (verification)")
    ap.add_argument("--herdr-workspace", type=Path, metavar="HERDR",
                    help="(the installer's handoff) wait for this Herdr to answer, then open the "
                         "HEADING OS workspace in --engine and run --heading /setup-wizard in it")
    ap.add_argument("--engine", type=Path, help=argparse.SUPPRESS)
    ap.add_argument("--heading", type=Path, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.herdr_workspace is not None:
        # Writes nothing under ~/.local/bin, so the yard refusal below does not apply.
        if not (args.engine and args.heading):
            ap.error("--herdr-workspace needs --engine and --heading")
        try:
            pane = open_in_herdr(args.herdr_workspace, args.engine, args.heading,
                                 env=dict(os.environ), wait_s=HERDR_WAIT_S)
        except (HerdrHandoffError, OSError, subprocess.SubprocessError) as exc:
            print(f"the setup wizard was not started in Herdr: {exc}", flush=True)
            return 1
        print(f"the setup wizard was started in Herdr pane {pane}", flush=True)
        return 0
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
