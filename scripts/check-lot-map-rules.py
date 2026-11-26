#!/usr/bin/env python3
"""check-lot-map-rules.py - the two copies of the lot-map station rules agree.

The rules that choose a station and a label for a tool call exist twice:
`shell_station` and `tool_station` in scripts/lot-map.py, which reads past
sessions from a transcript, and `shellStation` and `toolStation` in the lot-map
mod's scripts/claude-mods/lot-map/hooks/stations.ts, which runs live. This
checker runs every case of the corpus scripts/claude-mods/lot-map/station-cases.json
through both and exits 1 when either copy returns anything but the case's
"expect". The commit gate `lot-map-rules-agree` in .pre-commit-config.yaml runs
it whenever one of the copies, the corpus or this file changes (HOS-414).

The Python copy is loaded by path with importlib, under the default `Opts()`.
The TypeScript copy is imported by one `node` subprocess (Node 24 strips the
types itself, no build step); the cases go in on stdin and the results come
back on stdout, both as JSON. Node missing or failing is a failure: a check
that could not run never passes.

Usage:
    python scripts/check-lot-map-rules.py
    python scripts/check-lot-map-rules.py --lot-map-py <copy> --stations-ts <copy> --cases <file>

Exit codes: 0 every case agrees; 1 a mismatch, an unusable corpus, or a copy
that could not be loaded or run.

Persistence: none. Reads three files, writes nothing, listens on no port.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOT_MAP = ROOT / "scripts" / "lot-map.py"
DEFAULT_STATIONS = ROOT / "scripts" / "claude-mods" / "lot-map" / "hooks" / "stations.ts"
DEFAULT_CASES = ROOT / "scripts" / "claude-mods" / "lot-map" / "station-cases.json"

# The number of cases committed in the corpus; fewer is a failure, so a corpus
# emptied or cut short never passes.
MIN_CASES = 61

MODULE_KEY = "lot_map_under_check"

# Reads the cases from stdin, imports stations.ts from the file URL in argv,
# prints one [station, label] per case, in order.
NODE_DRIVER = r"""
let text = ''
process.stdin.setEncoding('utf8')
for await (const chunk of process.stdin) text += chunk
const m = await import(process.argv[1])
const cases = JSON.parse(text)
const out = cases.map(c => c.kind === 'shell' ? m.shellStation(c.command) : m.toolStation(c.tool, c.input))
process.stdout.write(JSON.stringify(out))
"""


class CheckError(Exception):
    """The check could not run."""


def load_cases(path: Path) -> list[dict]:
    try:
        cases = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CheckError(f"corpus {path} is missing or unreadable: {exc}") from exc
    if not isinstance(cases, list):
        raise CheckError(f"corpus {path} is not a list")
    if len(cases) < MIN_CASES or not cases:
        raise CheckError(f"corpus {path} holds {len(cases)} cases, fewer than {max(MIN_CASES, 1)}")
    for n, case in enumerate(cases):
        if not isinstance(case, dict) or case.get("kind") not in ("shell", "tool") \
                or not isinstance(case.get("expect"), list):
            raise CheckError(f"corpus {path} case {n} is malformed: {case!r}")
    return cases


def python_results(lot_map: Path, cases: list[dict]) -> list[list[str]]:
    if str(ROOT) not in sys.path[:1]:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(MODULE_KEY, lot_map)
    if spec is None or spec.loader is None:
        raise CheckError(f"cannot load {lot_map}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_KEY] = module  # its dataclass looks itself up there
    try:
        spec.loader.exec_module(module)
    except (OSError, ImportError, SyntaxError) as exc:
        raise CheckError(f"cannot load {lot_map}: {exc}") from exc
    opts = module.Opts()
    out = []
    for case in cases:
        if case["kind"] == "shell":
            got = module.shell_station(case["command"], opts)
        else:
            got = module.tool_station(case["tool"], case.get("input") or {}, opts)
        out.append(list(got))
    return out


def ts_results(stations: Path, cases: list[dict]) -> list[list[str]]:
    payload = [{"kind": c["kind"], "command": c.get("command", ""), "tool": c.get("tool", ""),
                "input": c.get("input") or {}} for c in cases]
    try:
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", NODE_DRIVER, stations.resolve().as_uri()],
            input=json.dumps(payload), capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CheckError(f"node could not run: {exc}") from exc
    if proc.returncode != 0:
        raise CheckError(f"node failed with exit {proc.returncode}: {proc.stderr.strip()}")
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise CheckError(f"node printed no JSON: {exc}: {proc.stdout[:200]!r}") from exc
    if not isinstance(out, list) or len(out) != len(cases):
        raise CheckError(f"node returned {len(out) if isinstance(out, list) else 'no'} results "
                         f"for {len(cases)} cases")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--lot-map-py", type=Path, default=DEFAULT_LOT_MAP)
    parser.add_argument("--stations-ts", type=Path, default=DEFAULT_STATIONS)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    args = parser.parse_args(argv)
    try:
        cases = load_cases(args.cases)
        py = python_results(args.lot_map_py, cases)
        ts = ts_results(args.stations_ts, cases)
    except CheckError as exc:
        print(f"lot-map rules check could not run: {exc}")
        return 1
    bad = 0
    for case, p, t in zip(cases, py, ts, strict=True):
        if p != case["expect"] or t != case["expect"]:
            bad += 1
            shown = {k: v for k, v in case.items() if k != "expect"}
            print(f"MISMATCH {json.dumps(shown)}\n  expected {case['expect']}\n  python   {p}\n  ts       {t}")
    if bad:
        print(f"lot-map rules disagree: {bad} of {len(cases)} cases")
        return 1
    print(f"lot-map rules agree: {len(cases)} cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
