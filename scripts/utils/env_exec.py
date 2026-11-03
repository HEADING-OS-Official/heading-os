#!/usr/bin/env python3
"""Start a program with the variables of an env file added: systemd's EnvironmentFile for launchd (HOS-341).

Usage (as the first words of a launchd agent's ProgramArguments, written by
scripts/utils/schedule.py, never by hand):
    <python> scripts/utils/env_exec.py [--optional] <env file> <program> [args...]

launchd has no EnvironmentFile, and a key must never sit in a plist (which is
readable by the user's other programs and lands in backups), so the agent runs
this first. It reads the file the way systemd reads the forms the engine writes:
one `KEY=VALUE` per line; blank lines and lines starting with `#` or `;`
skipped; a value wrapped in one pair of matching single or double quotes
unwrapped; no other expansion. Then it replaces itself with the program, so
launchd watches the program's own process.

It refuses, with one line on stderr naming the file and the reason and exit 78
(EX_CONFIG, which launchd logs), a file that is not a regular file, a file a
group or other user may read or write, and a line it cannot read as a variable.
`--optional` (systemd's leading `-`) treats a missing file as empty.

Standard library only: it runs under the units' interpreter before anything else.
"""

import os
import re
import stat
import sys

EX_CONFIG = 78
_NAME_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


class Refused(Exception):
    """The reason this file cannot be used, as one plain sentence."""


def read_env_file(path: str, optional: bool = False) -> dict[str, str]:
    """The variables `path` assigns, in file order; refuses by raising Refused."""
    try:
        # O_NONBLOCK: a FIFO with no writer would block the open itself, before the check below.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        if optional:
            return {}
        raise Refused("it does not exist") from None
    except OSError as exc:
        raise Refused(f"it cannot be opened: {exc.strerror}") from None
    st = os.fstat(fd)  # the file checked is the file read
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise Refused("it is not a regular file")
    if st.st_mode & 0o077:
        os.close(fd)
        raise Refused(f"its mode {stat.S_IMODE(st.st_mode):04o} lets a group or other user read or write it")
    with os.fdopen(fd, "rb") as fh:
        raw = fh.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused("it is not UTF-8 text") from None
    values: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line[0] in "#;":
            continue
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not _NAME_RE.match(key):
            raise Refused(f"line {number} is not KEY=VALUE")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def main(argv: list[str]) -> int:
    optional = bool(argv) and argv[0] == "--optional"
    if optional:
        argv = argv[1:]
    if len(argv) < 2:
        print("env_exec.py: usage: env_exec.py [--optional] <env file> <program> [args...]", file=sys.stderr)
        return EX_CONFIG
    path, program = argv[0], argv[1:]
    try:
        values = read_env_file(path, optional)
    except Refused as exc:
        print(f"env_exec.py: refused {path}: {exc}", file=sys.stderr)
        return EX_CONFIG
    env = dict(os.environ)
    env.update(values)
    try:
        os.execve(program[0], program, env)  # noqa: S606  # the argv the unit names, no shell
    except OSError as exc:
        print(f"env_exec.py: cannot start {program[0]} (env file {path}): {exc.strerror}", file=sys.stderr)
        return EX_CONFIG
    return EX_CONFIG  # not reached: execve replaces this process


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
