#!/usr/bin/env python3
"""Put a directory on the user's PATH for every new terminal: one helper.

The launcher (scripts/utils/heading_launcher.py) and the installer
(scripts/heading-install.py, HOS-301) both call `add_to_path`, so the
startup-file logic lives here once.

WHY NOT `~/.profile` ALONE. Measured on the Mint 22.3 test VM on 2026-11-01
(HELM's review of HOS-310): the stock `~/.profile` adds `~/.local/bin` only if
the directory exists at login, it did not, and a new terminal answered
"claude: command not found". Bash also reads only the FIRST of
`~/.bash_profile`, `~/.bash_login`, `~/.profile` at login, and zsh reads none
of them. So the line goes into the login file the user's shell actually reads
AND its interactive rc, and the directory is created first.

The directory is shell-quoted (`shlex.quote`), so nothing in its name expands
when the file is sourced; a name with a line break or `:` is refused.

The line is guarded (`case ":$PATH:"`), so a file sourced twice adds the
directory once, and each file gets it at most once, found by its marker.
Writes are atomic and keep the file's mode.

Usage (by hand):
    .venv/bin/python scripts/utils/shell_path.py ~/.local/bin --home DIR
"""
from __future__ import annotations

import argparse
import os
import pwd
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.utils.atomic import atomic_write_text  # noqa: E402

MARK = "# added by HEADING OS: PATH"


def login_shell() -> str:
    """The user's login shell name (`bash`, `zsh`, ...): $SHELL, else the passwd entry."""
    shell = os.environ.get("SHELL") or pwd.getpwuid(os.getuid()).pw_shell or "sh"
    return Path(shell).name


def startup_files(home: Path, shell: str) -> list[Path]:
    """The files a new terminal of `shell` reads, login file first."""
    if shell == "zsh":
        return [home / ".zprofile", home / ".zshrc"]
    if shell == "bash":
        # bash reads the first of these that exists, and only that one.
        login = next((home / n for n in (".bash_profile", ".bash_login", ".profile")
                      if (home / n).exists()), home / ".profile")
        return [login, home / ".bashrc"]
    return [home / ".profile"]


def _line(directory: Path, home: Path) -> str:
    """The marker and the guarded PATH line. The directory is shell-QUOTED, so a
    `$`, a space, a quote or a command substitution in its name stays literal;
    only `$HOME` and `$PATH` expand. A line break cannot be quoted onto one
    line, and a `:` would split the PATH entry, so both are refused."""
    if any(c in str(directory) for c in "\n\r:"):
        raise ValueError(f"refusing a directory name with a line break or ':': {directory!r}")
    try:
        rest = str(directory.relative_to(home))
        shown = f"$HOME/{rest}"
        expr = '"$HOME"/' + shlex.quote(rest)
    except ValueError:
        shown = str(directory)
        expr = shlex.quote(shown)
    return (f"{MARK} {shown}\n"
            f'case ":$PATH:" in *:{expr}:*) ;; *) PATH={expr}:"$PATH"; export PATH ;; esac\n')


def add_to_path(directory: Path, *, home: Path | None = None,
                login: str | None = None) -> list[Path]:
    """Create `directory` and add it to PATH in the user's startup files.

    Returns the files this call changed; empty when every file already had it.
    """
    home = (home or Path.home()).resolve()
    directory = Path(directory).expanduser().resolve()
    line = _line(directory, home)  # refuses a bad name before anything is created
    directory.mkdir(parents=True, exist_ok=True)
    marker = line.splitlines()[0]
    changed = []
    for path in startup_files(home, login or login_shell()):
        try:
            text = path.read_text(encoding="utf-8")
            mode = path.stat().st_mode & 0o777
        except FileNotFoundError:
            text, mode = "", 0o644
        if marker in text.splitlines():
            continue
        sep = "" if not text or text.endswith("\n") else "\n"
        atomic_write_text(path, text + sep + line, mode=mode)
        changed.append(path)
    return changed


def on_path(directory: Path, path_env: str | None = None) -> bool:
    """Whether `directory` is on PATH in THIS process (not in a new terminal)."""
    path_env = os.environ.get("PATH", "") if path_env is None else path_env
    target = Path(directory).expanduser().resolve()
    return any(Path(p).expanduser().resolve() == target
               for p in path_env.split(os.pathsep) if p)


def is_real_home(home: Path) -> bool:
    """`--home` naming the user's own home (passwd entry or $HOME), not a scratch one."""
    real = {Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(), Path.home().resolve()}
    return Path(home).expanduser().resolve() in real


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("directory", type=Path)
    ap.add_argument("--home", type=Path, required=True,
                    help="the home whose startup files to edit")
    ap.add_argument("--shell", help="login shell name (default: the user's)")
    args = ap.parse_args(argv)
    if is_real_home(args.home):
        # The real startup files must name the main clone's paths, never a yard's.
        from scripts.utils.clone_guard import require_main_clone
        require_main_clone(__file__)
    for path in add_to_path(args.directory, home=args.home, login=args.shell):
        print(f"added to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
