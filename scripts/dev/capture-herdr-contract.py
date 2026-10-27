#!/usr/bin/env python3
"""Capture the herdr CLI grammar this engine depends on, as a data file.

Usage:
    python scripts/dev/capture-herdr-contract.py            # print to stdout
    python scripts/dev/capture-herdr-contract.py --write    # write the contract file

WHY THIS EXISTS. Until 2026-09-03 no test in this repository compared a herdr
argument against the real CLI or its schema. The stub in `tests/conftest.py`
recorded argv and exited 0 for anything, so it confirmed every shape it was
given, including three wrong ones that reached production: a payload key one
level above where herdr puts it, a `result.workspace.cwd` that exists at no
depth, and a `worktree create` positional the command does not accept.

The contract is CAPTURED, never hand-written, so it cannot drift into whatever
the engine happens to send. Nothing compares it automatically: run this script
with `--write` after any herdr upgrade and read the diff against the previous
capture. `--write` puts it under the data registry name `reference-docs`.

Deliberately narrow: only the commands this engine actually invokes. A wider
capture would be a bigger file that goes stale in more places, and nothing reads
the rest.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CONTRACT_NAME = "herdr-cli-contract.json"

# Every herdr command the engine runs, and where from. Keep the comment: a
# command that loses its caller should leave this list rather than linger.
COMMANDS = {
    "pane list": "yard-bootstrap.sh, resolving the target pane",
    "pane get": "yard-bootstrap.sh, checking the pane before autostart",
    "pane report-metadata": "yard-bootstrap.sh badge()",
    "pane run": "yard-bootstrap.sh autostart",
    "notification show": "yard-bootstrap.sh notify()",
    "workspace get": "yard-bootstrap.sh, the recovery path",
    "workspace rename": "yard-bootstrap.sh, labelling the YARD",
    "workspace list": "scripts/herdr-brief.py, resolving a yard's workspace",
    "worktree create": "the only way a YARD is created; the herdr README (spec-docs)",
    "worktree remove": ".claude/hooks/_dispatch.py parses its --workspace form",
    "agent start": ".claude/hooks/_dispatch.py parses its --pane form",
    "agent get": "scripts/herdr-brief.py, confirming the addressed pane",
    "agent prompt": "scripts/herdr-brief.py and scripts/utils/herdr_agent.py",
    "agent list": "scripts/utils/herdr_agent.py",
    "agent rename": "scripts/utils/herdr_agent.py set_label()",
    "plugin config-dir": "yard-bootstrap.sh, the operator's switch file",
}

OPTION = re.compile(r"^\s+(--[a-z0-9-]+)(?:[ =]+<([^>]+)>)?\s*$")


def _help(command: str) -> str:
    proc = subprocess.run(["herdr", *command.split(), "--help"],
                          capture_output=True, text=True, timeout=60)
    # Some builds print help on stderr; take whichever carries the usage line.
    for stream in (proc.stdout, proc.stderr):
        if "Usage:" in stream:
            return stream
    raise SystemExit(f"no usage line for {command!r}:\n{proc.stdout}{proc.stderr}")


def _parse(command: str, text: str) -> dict:
    usage = next(line for line in text.splitlines() if line.startswith("Usage:"))
    tail = usage.split(command, 1)[1] if command in usage else ""

    minimum = maximum = 0
    variadic = False
    skip_next = False
    for word in tail.split():
        if skip_next:
            # The VALUE of a required option shown in the usage line, e.g.
            # `pane report-metadata [OPTIONS] --source <ID> <PANE_ID>`. Counting
            # it as a positional made that command look like it took two.
            skip_next = False
            continue
        if word == "[OPTIONS]":
            continue
        if word.startswith("--"):
            skip_next = True
            continue
        bare = word.rstrip(".")
        if word.endswith("..."):
            variadic = True
        if "|" in bare:
            # `<NAME>|--clear`: the positional has an alternative, so it is not
            # required. `agent rename` is the only one, and its `Arguments:`
            # section agrees by printing `[NAME]`.
            maximum += 1
        elif bare.startswith("<"):
            minimum += 1
            maximum += 1
        elif bare.startswith("["):
            maximum += 1

    options = {}
    for line in text.splitlines():
        found = OPTION.match(line)
        if found:
            options[found.group(1)] = bool(found.group(2))

    return {
        "options": options,
        "positionals": {"min": minimum,
                        "max": None if variadic else maximum},
        "called_from": COMMANDS[command],
    }


def capture() -> dict:
    version = subprocess.run(["herdr", "--version"], capture_output=True,
                             text=True, timeout=60).stdout.strip()
    return {
        "_provenance":
            "Generated by scripts/dev/capture-herdr-contract.py from `herdr "
            "<cmd> --help`. Do not edit by hand: re-run the script after a "
            "herdr upgrade and read the diff.",
        "herdr_version": version,
        "captured": datetime.now(timezone.utc).date().isoformat(),
        "commands": {name: _parse(name, _help(name)) for name in COMMANDS},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true",
                        help=f"write {CONTRACT_NAME} under the data registry "
                             "name reference-docs")
    args = parser.parse_args()

    contract = json.dumps(capture(), indent=2, sort_keys=True) + "\n"
    if args.write:
        from scripts.utils.atomic import atomic_write_text
        from scripts.utils.data_path import data_path

        target = data_path("reference-docs") / CONTRACT_NAME
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, contract)
        print(f"wrote {target}")
    else:
        sys.stdout.write(contract)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
