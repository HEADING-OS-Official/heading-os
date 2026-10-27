#!/usr/bin/env python3
"""Guard: an installed systemd user unit that was rendered from an OLDER template.

Usage:
    python scripts/check-unit-drift.py                  # report every installed unit
    python scripts/check-unit-drift.py --check          # exit 1 on directive drift
    python scripts/check-unit-drift.py --json
    python scripts/check-unit-drift.py --unit nightly-refresh.service   # repeatable

The question this asks, and the one it replaces
-----------------------------------------------
`install-nightly-refresh-timer.sh --check` asks whether the timer is ARMED: three
reboot-survival mechanisms, plus a grep for a `{{TOKEN}}` that survived
substitution. Every one of those can answer yes over a unit file that is weeks
older than the template it came from, because none of them compares the two. The
question here is the other one: **was this file on disk produced by THIS
template?**

Why it exists: an installed `nightly-refresh.service` was rendered hours before
`Environment=PATH={{TOOLPATH}}` entered its template (`355e3927`), the installer
was not re-run, and the nightly failed every night for sixteen days for a cause
the repository already fixed (measured 2026-09-21).

Comment drift is REPORTED and does not fail
--------------------------------------------
A `#` line in a unit changes nothing systemd does. It is still proof the
installer has not been re-run since the template changed, so it is printed as
`[stale]` and counted; it is not what `--check` exits non-zero for. Two reasons
to draw the line there rather than one step earlier. Re-rendering a unit is a
machine action, which in this workspace belongs to HELM and to the operator's own
word, so a red line demands one for a change that alters nothing. And a check
that goes red over a reworded comment is a check people learn to scroll past,
which costs the directive findings their audience. `--json` and the
report both carry comment drift in full, so nothing is hidden by the choice.

What it compares, exactly
-------------------------
The template's text and the unit's text, line by line, aligned by longest common
subsequence, where a template line containing `{{TOKEN}}` matches any disk line
that agrees on everything OUTSIDE the token. So every byte of the template that
is not a placeholder is compared, in order: directives, their order, blank lines
and comments alike. What is deliberately ignored is the substituted VALUE.

What it does NOT catch, each on its own line because each is a real hole
-----------------------------------------------------------------------
A wrong value inside a correctly shaped unit: `{{WORKSPACE}}` pointing at a yard
worktree deleted two days ago, `{{PYTHON}}` pointing at the system interpreter
instead of the pinned `.venv`, a `{{TOOLPATH}}` that no longer resolves `node`.
The placeholder is a wildcard and any value passes it.

A drop-in (`<unit>.d/*.conf`) that overrides a directive this check just
verified. Two units on this machine have one. They are COUNTED and named in the
report so the blindness is visible, but their contents are neither read nor
compared.

systemd's loaded state. A unit file can match its template perfectly and still
not be what is running, because the file was written after the last
`daemon-reload`. Only `systemctl --user cat` answers that, and asking it is a
read this check deliberately does not need.

A template with no unit installed, which is the "not installed" question each
installer's own `--check` already owns.

A template line carrying the SAME token twice, where the two occurrences resolved
to different values on disk. Each `{{TOKEN}}` becomes an independent wildcard, so
that line matches.

A unit with no template at all is neither passed nor failed: it is an installed
program this workspace talks to rather than a thing it renders, and it is listed
as out of scope. The report prints how many there are on the machine it runs on.

Tests: none live since the 2026-09-21 test-suite split. The case that mattered
was an installed unit still carrying a template the repository had since fixed,
which is what this script exists to surface; the detection is unchanged, and the
proof that it catches that case is what left.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TEMPLATES = ROOT / "scripts" / "templates" / "systemd"
UNIT_SUFFIXES = (".service", ".timer", ".path", ".socket")
TOKEN_RE = re.compile(r"\{\{[A-Z_]+\}\}")

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_ERROR = 2

_RX_CACHE: dict[str, re.Pattern[str]] = {}


def default_units_dir() -> Path:
    """`~/.config/systemd/user`, derived from the running user's home.

    Never a literal: this repository is public, and a test must be able to point
    the whole check at a `tmp_path` tree (`--units-dir`) rather than reading the
    operator's real units, which answer differently on a machine that has none.
    """
    return Path.home() / ".config" / "systemd" / "user"


def _line_pattern(template_line: str) -> re.Pattern[str]:
    cached = _RX_CACHE.get(template_line)
    if cached is not None:
        return cached
    parts = TOKEN_RE.split(template_line)
    source = "".join(
        re.escape(part) + (".*" if i < len(parts) - 1 else "")
        for i, part in enumerate(parts)
    )
    pattern = re.compile(source)
    _RX_CACHE[template_line] = pattern
    return pattern


def line_matches(template_line: str, disk_line: str) -> bool:
    """True when `disk_line` could have been rendered from `template_line`."""
    return _line_pattern(template_line).fullmatch(disk_line) is not None


def is_directive(line: str) -> bool:
    """A line systemd acts on, as opposed to a comment or a blank."""
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith(("#", ";"))


def align(template_lines: list[str], disk_lines: list[str]) -> list[tuple[str, int | None, int | None]]:
    """Longest-common-subsequence alignment under `line_matches`.

    `difflib` cannot do this: it decides equality by hashing, and a template line
    is equal to its rendered form only under a regex. The corpus is one unit file
    against one template, tens of lines each, so the quadratic table is free.

    Returns operations in file order: ("same", t, d), ("missing", t, None) for a
    template line absent from the unit, ("extra", None, d) for a unit line the
    template does not account for.
    """
    n, m = len(template_lines), len(disk_lines)
    table = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        row, nxt = table[i], table[i + 1]
        t_line = template_lines[i]
        for j in range(m - 1, -1, -1):
            if line_matches(t_line, disk_lines[j]):
                row[j] = nxt[j + 1] + 1
            else:
                row[j] = max(nxt[j], row[j + 1])

    ops: list[tuple[str, int | None, int | None]] = []
    i = j = 0
    while i < n and j < m:
        if line_matches(template_lines[i], disk_lines[j]):
            ops.append(("same", i, j))
            i += 1
            j += 1
        elif table[i + 1][j] >= table[i][j + 1]:
            ops.append(("missing", i, None))
            i += 1
        else:
            ops.append(("extra", None, j))
            j += 1
    while i < n:
        ops.append(("missing", i, None))
        i += 1
    while j < m:
        ops.append(("extra", None, j))
        j += 1
    return ops


def compare(template_text: str, disk_text: str) -> dict[str, Any]:
    """Classify one unit against one template.

    verdict is `match`, `comment-drift` or `directive-drift`; `findings` names
    every line that differs, so the report can say WHAT is missing rather than
    that something is.
    """
    template_lines = template_text.splitlines()
    disk_lines = disk_text.splitlines()
    findings: list[dict[str, Any]] = []
    for kind, t_index, d_index in align(template_lines, disk_lines):
        if kind == "same":
            continue
        if kind == "missing":
            line = template_lines[t_index]  # type: ignore[index]
            findings.append({
                "kind": "missing_from_unit",
                "template_line": t_index + 1,  # type: ignore[operator]
                "text": line,
                "directive": is_directive(line),
            })
        else:
            line = disk_lines[d_index]  # type: ignore[index]
            findings.append({
                "kind": "absent_from_template",
                "unit_line": d_index + 1,  # type: ignore[operator]
                "text": line,
                "directive": is_directive(line),
            })
    if not findings:
        verdict = "match"
    elif any(f["directive"] for f in findings):
        verdict = "directive-drift"
    else:
        verdict = "comment-drift"
    return {"verdict": verdict, "findings": findings}


def dropins_for(unit_path: Path) -> list[str]:
    """Names of the drop-in fragments that override this unit, if any."""
    d = unit_path.with_name(unit_path.name + ".d")
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.suffix == ".conf")


def survey(units_dir: Path, templates_dir: Path,
           only: list[str] | None = None) -> dict[str, Any]:
    """Compare every installed unit (or the named ones) against its template."""
    results: list[dict[str, Any]] = []
    errors: list[str] = []

    if not units_dir.is_dir():
        return {"units_dir": str(units_dir), "templates_dir": str(templates_dir),
                "results": [], "errors": [f"no unit directory: {units_dir}"]}
    if not templates_dir.is_dir():
        return {"units_dir": str(units_dir), "templates_dir": str(templates_dir),
                "results": [], "errors": [f"no template directory: {templates_dir}"]}

    if only:
        paths = []
        for name in only:
            candidate = units_dir / name
            if not candidate.is_file():
                # An absent unit REFUSES. A check asked about a specific unit and
                # answering "fine" because the file is not there is the shape
                # this whole script exists against.
                errors.append(f"not installed: {candidate}")
                continue
            paths.append(candidate)
    else:
        paths = sorted(p for p in units_dir.iterdir()
                       if p.is_file() and p.suffix in UNIT_SUFFIXES)

    for path in paths:
        template = templates_dir / path.name
        entry: dict[str, Any] = {"unit": path.name, "path": str(path),
                                 "dropins": dropins_for(path)}
        if not template.is_file():
            entry["verdict"] = "no-template"
            entry["findings"] = []
            results.append(entry)
            continue
        try:
            disk_text = path.read_text(encoding="utf-8")
            template_text = template.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # Read failures REFUSE rather than pass. Reported per unit and
            # counted as an error, never as a match.
            entry["verdict"] = "unreadable"
            entry["findings"] = []
            entry["error"] = f"{type(exc).__name__}: {exc}"
            errors.append(f"unreadable: {path}: {exc}")
            results.append(entry)
            continue
        entry.update(compare(template_text, disk_text))
        entry["template"] = str(template)
        results.append(entry)

    return {"units_dir": str(units_dir), "templates_dir": str(templates_dir),
            "results": results, "errors": errors}


def counts(survey_result: dict[str, Any]) -> dict[str, int]:
    tally = {"match": 0, "comment-drift": 0, "directive-drift": 0,
             "no-template": 0, "unreadable": 0}
    for entry in survey_result["results"]:
        tally[entry["verdict"]] = tally.get(entry["verdict"], 0) + 1
    tally["template-rendered"] = (tally["match"] + tally["comment-drift"]
                                  + tally["directive-drift"])
    return tally


_INSTALLER_GLOBS = ("install-*.sh", "install-*.py")


def installer_for(unit: str, scripts_dir: Path = ROOT / "scripts") -> str | None:
    """The installer that renders `unit`, as a command line, or None.

    `install-<stem>-timer.sh` when it exists; otherwise the single installer
    whose text names the unit stem as a whole word. Two or more candidates, or
    none, answer None: a guessed installer is worse than "its installer".
    """
    stem = unit.rsplit(".", 1)[0]
    direct = scripts_dir / f"install-{stem}-timer.sh"
    if direct.is_file():
        return f"bash scripts/{direct.name}"
    word = re.compile(rf"(?<![\w-]){re.escape(stem)}(?![\w-])")
    hits = []
    for pattern in _INSTALLER_GLOBS:
        for path in sorted(scripts_dir.glob(pattern)):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                print(f"  [warn] cannot read {path}: {exc}", file=sys.stderr)
                continue
            if word.search(text):
                hits.append(path)
    if len(hits) != 1:
        return None
    runner = "bash" if hits[0].suffix == ".sh" else ".venv/bin/python"
    return f"{runner} scripts/{hits[0].name}"


def render(survey_result: dict[str, Any], installer_hint: bool = True) -> str:
    lines: list[str] = []
    tally = counts(survey_result)
    for entry in survey_result["results"]:
        verdict = entry["verdict"]
        if verdict == "match":
            lines.append(f"  [ok] {entry['unit']} matches its template")
        elif verdict == "no-template":
            lines.append(f"  [skip] {entry['unit']} has no template here (out of scope)")
        elif verdict == "unreadable":
            lines.append(f"  [ERROR] {entry['unit']}: {entry['error']}")
        else:
            label = "STALE" if verdict == "comment-drift" else "DRIFT"
            lines.append(f"  [{label}] {entry['unit']} was rendered from an OLDER template")
            for finding in entry["findings"]:
                mark = "directive" if finding["directive"] else "comment  "
                if finding["kind"] == "missing_from_unit":
                    where = f"template line {finding['template_line']}"
                    lines.append(f"         - {mark}  missing from the unit   ({where}): {finding['text']}")
                else:
                    where = f"unit line {finding['unit_line']}"
                    lines.append(f"         - {mark}  not in the template     ({where}): {finding['text']}")
        if entry["dropins"]:
            lines.append(f"         note: {len(entry['dropins'])} drop-in(s) override this unit "
                         f"and are NOT compared: {', '.join(entry['dropins'])}")
    lines.append("")
    lines.append(
        f"  {tally['template-rendered']} template-rendered, {tally['match']} match, "
        f"{tally['directive-drift']} directive drift, {tally['comment-drift']} comment-only drift, "
        f"{tally['no-template']} out of scope, {tally['unreadable']} unreadable")
    if installer_hint and tally["directive-drift"]:
        lines.append("")
        lines.append("  A drifted unit is re-rendered by re-running its own installer, in HELM,")
        lines.append("  on the operator's word:")
        for entry in survey_result["results"]:
            if entry["verdict"] != "directive-drift":
                continue
            command = installer_for(entry["unit"])
            lines.append(f"    {entry['unit']}: "
                         + (command or "its installer (none found by name under scripts/)"))
    for error in survey_result["errors"]:
        lines.append(f"  [ERROR] {error}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="exit 1 on directive drift, 2 on an error")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    parser.add_argument("--unit", action="append", default=None, metavar="NAME",
                        help="scope to one unit file name; repeatable")
    parser.add_argument("--units-dir", type=Path, default=None,
                        help="default ~/.config/systemd/user")
    parser.add_argument("--templates-dir", type=Path, default=DEFAULT_TEMPLATES)
    args = parser.parse_args(argv)

    units_dir = args.units_dir if args.units_dir is not None else default_units_dir()
    result = survey(units_dir, args.templates_dir, only=args.unit)
    tally = counts(result)

    if args.json:
        print(json.dumps({**result, "counts": tally}, indent=2))
    else:
        print(render(result))

    if result["errors"]:
        return EXIT_ERROR
    if args.check and tally["directive-drift"]:
        return EXIT_DRIFT
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
