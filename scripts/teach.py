#!/usr/bin/env python3
"""The /teach skill's only door to disk: where a course lives, and nowhere else.

A /teach course (mission, resources, lessons, reference sheets, learning
records) is the operator's private learning record, so it lives in the data
repository under the registry name `teach-courses`, one subdirectory per course.
The upstream skill this one adapts (Matt Pocock's `teach`, MIT) writes into the
current directory; run from the engine checkout that would put private data
into the public repository. So the skill never builds a path itself: it asks
this script, and this script refuses when there is no data repository or when
the answer would land inside the engine checkout. Operator request 2026-10-28:
the skill must not be able to run from the engine, and its results must always
be in the data repository.

Usage:
    .venv/bin/python scripts/teach.py where [COURSE]
    .venv/bin/python scripts/teach.py init COURSE
    .venv/bin/python scripts/teach.py next COURSE lessons|reference|learning-records
    .venv/bin/python scripts/teach.py check PATH
    .venv/bin/python scripts/teach.py list

Every path is printed twice, the Linux path and the Windows `\\\\wsl.localhost`
path from `wslpath -w`. Exit 2 on a refusal, with the reason on stderr.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.data_path import DataPathError, data_path  # noqa: E402
from scripts.utils.paths import (  # noqa: E402
    DataRootError,
    require_outside_engine_clone,
    require_writable_data_root,
)

REGISTRY_NAME = "teach-courses"
COURSE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# The numbered directories of a course and the extension each holds.
NUMBERED = {"lessons": ".html", "reference": ".html", "learning-records": ".md"}
SUBDIRS = ("lessons", "reference", "learning-records", "assets")
NUMBER_RE = re.compile(r"^(\d+)-")


class Refused(Exception):
    """A write this script will not answer for; the message says why."""


def courses_root() -> Path:
    """`teach-courses`, after both refusals: no data repository, or inside the engine."""
    require_writable_data_root()
    root = data_path("teach-courses")
    return require_outside_engine_clone(root, f"The registry name {REGISTRY_NAME!r}")


def course_dir(course: str) -> Path:
    if not COURSE_RE.fullmatch(course):
        raise Refused(f"course name {course!r} is not kebab-case "
                      "(lowercase letters, digits and hyphens, at most 63 characters)")
    target = courses_root() / course
    inside_courses(target)
    return target


def inside_courses(path: str | Path) -> Path:
    """`path` RESOLVED, or Refused when it is not under `teach-courses` or is in the engine.

    Asked of the resolved path, so a symlinked course or subdirectory that
    leads out of the root, into the engine checkout or anywhere else, is
    refused before anything follows it.
    """
    root = courses_root().resolve()
    resolved = Path(path).expanduser().resolve()
    if resolved != root and root not in resolved.parents:
        raise Refused(f"{resolved} is outside {root}; a /teach file is written "
                      f"only under the registry name {REGISTRY_NAME!r}")
    return require_outside_engine_clone(resolved, f"The /teach path {path}")


def windows_path(path: Path) -> str:
    if not shutil.which("wslpath"):
        return "(wslpath not found, no Windows path)"
    done = subprocess.run(["wslpath", "-w", str(path)], capture_output=True,
                          text=True, check=False)
    return done.stdout.strip() or f"(wslpath failed: {done.stderr.strip()})"


def show(path: Path) -> None:
    print(path)
    print(windows_path(path))


def next_number(directory: Path) -> int:
    numbers = [int(m.group(1)) for p in directory.glob("*")
               if (m := NUMBER_RE.match(p.name))] if directory.is_dir() else []
    return max(numbers, default=0) + 1


def cmd_where(args) -> int:
    show(course_dir(args.course) if args.course else courses_root())
    return 0


def cmd_init(args) -> int:
    target = course_dir(args.course)
    existed = target.is_dir()
    for sub in SUBDIRS:
        inside_courses(target / sub)
    for sub in SUBDIRS:
        (target / sub).mkdir(parents=True, exist_ok=True)
    print("course exists, nothing overwritten" if existed else "course created")
    show(target)
    return 0


def cmd_next(args) -> int:
    target = course_dir(args.course)
    if not target.is_dir():
        raise Refused(f"no course {args.course!r}; run `teach.py init {args.course}` first")
    directory = inside_courses(target / args.kind)
    n = next_number(directory)
    print(f"{n:04d}")
    print(f"{directory}/{n:04d}-<dash-case-name>{NUMBERED[args.kind]}")
    return 0


def cmd_check(args) -> int:
    show(inside_courses(args.path))
    return 0


def cmd_list(_args) -> int:
    root = courses_root()
    courses = sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    if not courses:
        print(f"no courses under {root}")
        return 0
    for c in courses:
        lessons = len(list((c / "lessons").glob("*.html")))
        records = len(list((c / "learning-records").glob("*.md")))
        mission = "mission" if (c / "MISSION.md").is_file() else "NO MISSION"
        print(f"{c.name}: {lessons} lesson(s), {records} learning record(s), {mission}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("where", help="the courses root, or one course's directory")
    p.add_argument("course", nargs="?")
    p.set_defaults(func=cmd_where)
    p = sub.add_parser("init", help="create a course's directories; never overwrites")
    p.add_argument("course")
    p.set_defaults(func=cmd_init)
    p = sub.add_parser("next", help="the next sequence number in a numbered directory")
    p.add_argument("course")
    p.add_argument("kind", choices=sorted(NUMBERED))
    p.set_defaults(func=cmd_next)
    p = sub.add_parser("check", help="exit 0 only when PATH is under teach-courses")
    p.add_argument("path")
    p.set_defaults(func=cmd_check)
    p = sub.add_parser("list", help="every course with its lesson and record counts")
    p.set_defaults(func=cmd_list)
    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except (Refused, DataRootError, DataPathError) as exc:
        print(f"teach: refused. {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
