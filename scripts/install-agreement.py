#!/usr/bin/env python3
"""The agreement check: do the install's parts agree, for every intake answer? (HOS-312)

For each of seven answer combinations (mail exchange, google or none, times
GitHub backup yes or later, all with the Claude sign-in for the model proxy
`now`, and once more mail none, GitHub later, sign-in later) it writes a
scratch engine `.env` holding those answers and checks four things:

  A. every unit the families of that combination enable has a doctor row:
     the mandatory families a fresh install turns on (`select_families(
     mandatory_only=True)`, filtered for this platform and machine as
     `unit_rows` in scripts/doctor.py filters them), plus the two Exchange
     mail families when mail is exchange (scripts/utils/install_mail.py
     FAMILIES). The doctor's units are what `unit_rows` returns for THIS
     machine's platform: a check run on WSL checks the WSL list;
  B. every daemon the watchdog expects (`watchdog_core.load_expected` on the
     scratch engine) maps to a family of A;
  C. every ExecStart= of those families, rendered by
     `scripts/heading-services.py render ... --to <scratch>`, names its
     program by absolute path (Linux only; elsewhere skipped and said so);
  D. the Hindsight MCP bridge command and the Lific MCP launcher name their
     programs by absolute path.

It reads only the engine tree it lives in and the scratch folder it is given
(the render of C resolves its data addresses against an empty data root there),
installs nothing, starts nothing and asks no service manager. Run with the
engine's own `.venv/bin/python`. `scripts/heading-install.py --dry-run` runs it
as its last part.

Usage:
    .venv/bin/python scripts/install-agreement.py [--scratch DIR]

  --scratch DIR   where the scratch engines and renders go (default: this
                  checkout's scratch, `scripts/data-path.py scratch`, plus
                  /install-agreement); each run works in a fresh run-XXXX
                  folder of its own there and removes it at the end, and
                  nothing else in DIR is touched

Output: one line per combination, `agreement <mail>/<github>/<proxy>: ok` or
`...: N finding(s)` with each finding indented beneath; last line
`install-agreement: CLEAN, 0 findings` or `install-agreement: N findings`.
Exit 0 clean, 1 findings, 2 refused (the scratch cannot be used).
"""
from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))

from scripts.utils import install_answers  # noqa: E402

#: The answer variables removed from this process at start, so every reader sees only the scratch .env.
ANSWER_ENV = (install_answers.MAIL_ENV, install_answers.GITHUB_BACKUP_ENV, install_answers.TZ_ENV,
              install_answers.PROXY_SIGNIN_ENV)
COMBINATIONS = ([(mail, github, "now") for mail in ("exchange", "google", "none") for github in ("yes", "later")]
                + [("none", "later", "later")])
DAEMON_INSTALLER = "scripts/install-daemon-service.sh"
SYSTEM_PROGRAMS = ("/bin/sh", "/bin/bash")


def families_for(mail: str, platform: str):
    """Check A's families: what the install turns on for this mail answer, on this platform."""
    from scripts.utils.install_mail import FAMILIES as MAIL_FAMILIES
    from scripts.utils.schedule import select_families

    fams = [f for f in select_families(mandatory_only=True) if platform in f.platforms and f.this_machine]
    if mail == "exchange":
        fams += select_families(list(MAIL_FAMILIES))
    return fams


def _absolute(word: str) -> bool:
    return word in SYSTEM_PROGRAMS or word.startswith("/usr/bin/") or Path(word).is_absolute()


def check_a(fams, platform: str) -> list[str]:
    from scripts.doctor import unit_rows

    rows = {row[1] for row in unit_rows(platform)}
    return [f"A: unit {name} (family {fam.name}) has no doctor row"
            for fam in fams for name in fam.enable_units if name.rpartition(".")[0] not in rows]


def daemon_families() -> dict[str, str]:
    """Watchdog daemon name -> service family, read from the service layer's manifest.

    A daemon family (`_daemon_family` in scripts/utils/schedule.py) is installed
    by DAEMON_INSTALLER with the daemon's name as its second word, the name the
    watchdog uses; a watchdog name no daemon family carries is reported unknown.
    """
    from scripts.utils.schedule import SERVICE_FAMILIES

    return {f.installer[1]: f.name for f in SERVICE_FAMILIES
            if f.installer and len(f.installer) == 2 and f.installer[0] == DAEMON_INSTALLER}


def check_b(engine: Path, fams) -> list[str]:
    from scripts.watchdog_core import load_expected

    on = {f.name for f in fams}
    by_name = daemon_families()
    findings = []
    for daemon in load_expected(engine, cfg={}):
        family = by_name.get(daemon)
        if family is None:
            findings.append(f"B: unknown daemon name {daemon}")
        elif family not in on:
            findings.append(f"B: the watchdog expects {daemon}, whose family {family} this install leaves off")
    return findings


def check_c(fams, dest: Path, platform: str, data: Path, home: Path) -> tuple[list[str], str | None]:
    """(findings, a note when skipped).

    The render runs with HEADING_OS_DATA at an empty scratch data root and HOME
    at the scratch home, so it resolves no address in this machine's data and
    works the same on a machine that has none yet (a dry run before the install).
    """
    if platform != "linux":
        return [], f"agreement C: skipped on {platform} (no ExecStart lines)"
    proc = subprocess.run([sys.executable, str(ENGINE / "scripts" / "heading-services.py"), "render",
                           *[f.name for f in fams], "--to", str(dest)],
                          cwd=str(ENGINE), capture_output=True, text=True, errors="replace",
                          env=dict(os.environ, HEADING_OS_DATA=str(data), HOME=str(home)))
    if proc.returncode != 0:
        why = (proc.stderr.strip().splitlines() or [f"exit {proc.returncode}"])[-1]
        return [f"C: heading-services.py render failed: {why}"], None
    findings, checked = [], 0
    for unit in sorted(dest.iterdir()):
        for line in unit.read_text(encoding="utf-8").splitlines():
            if not line.startswith("ExecStart="):
                continue
            checked += 1
            # systemd's own prefixes (-, @, :, +, !) come before the program.
            words = line[len("ExecStart="):].lstrip("-@:+!").split()
            if not words or not _absolute(words[0]):
                findings.append(f"C: {unit.name} runs {words[0] if words else '(nothing)'} by bare name")
    if not checked:
        findings.append("C: the render holds no ExecStart= line to check")
    return findings, None


def check_d(home: Path) -> list[str]:
    from scripts.utils.install_hindsight import bridge_command
    from scripts.utils.install_lific import LificError, binary_path, launcher_path, launcher_text

    findings = []
    command, _args = bridge_command(types.SimpleNamespace(engine=ENGINE))
    if not _absolute(command):
        findings.append(f"D: the Hindsight MCP bridge runs {command} by bare name")
    launcher = launcher_path(home)
    if not launcher.is_absolute():
        findings.append(f"D: the Lific launcher is written at {launcher}, not an absolute path")
    try:
        text = launcher_text(home, binary_path(home))
    except (OSError, LificError) as exc:
        return findings + [f"D: the Lific launcher cannot be rendered: {exc}"]
    execs = 0
    for line in text.splitlines():
        words = shlex.split(line, comments=True) if line.strip().startswith("exec") else []
        if words[:1] != ["exec"]:
            continue
        execs += 1
        if len(words) < 2 or not _absolute(words[1]):
            findings.append(f"D: the Lific launcher execs {words[1] if len(words) > 1 else '(nothing)'} by bare name")
    if not execs:
        findings.append("D: the Lific launcher holds no exec line to check")
    return findings


def check_combination(scratch: Path, mail: str, github: str, proxy: str, platform: str) -> tuple[list[str], list[str]]:
    """(findings, notes) for one combination, under its own scratch folder."""
    root = scratch / f"{mail}-{github}-{proxy}"
    engine, home, render, data = root / "engine", root / "home", root / "render", root / "data"
    for d in (engine, home, render, data):
        d.mkdir(parents=True)
    env_file = engine / ".env"
    env_file.write_text(f"{install_answers.MAIL_ENV}={mail}\n{install_answers.GITHUB_BACKUP_ENV}={github}\n"
                        f"{install_answers.PROXY_SIGNIN_ENV}={proxy}\n", encoding="utf-8")
    os.chmod(env_file, 0o600)
    fams = families_for(mail, platform)
    findings = check_a(fams, platform) + check_b(engine, fams)
    c_findings, note = check_c(fams, render, platform, data, home)
    findings += c_findings + check_d(home)
    return findings, [note] if note else []


def default_scratch() -> Path:
    from scripts.utils.data_path import checkout_scratch_dir

    return checkout_scratch_dir(ENGINE) / "install-agreement"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--scratch", type=Path, default=None, metavar="DIR",
                    help="where the scratch engines and renders go (default: this checkout's scratch)")
    args = ap.parse_args(argv)
    for name in ANSWER_ENV:
        os.environ.pop(name, None)
    scratch = (args.scratch or default_scratch()).expanduser().absolute()
    try:
        scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
        run = Path(tempfile.mkdtemp(prefix="run-", dir=scratch))  # mode 700, this run's own
    except OSError as exc:
        print(f"install-agreement: the scratch folder {scratch} cannot be used: {exc}", file=sys.stderr)
        return 2
    platform = sys.platform
    total = 0
    try:
        for mail, github, proxy in COMBINATIONS:
            findings, notes = check_combination(run, mail, github, proxy, platform)
            total += len(findings)
            label = f"agreement {mail}/{github}/{proxy}"
            print(f"{label}: ok" if not findings else f"{label}: {len(findings)} finding(s)")
            for line in notes + findings:
                print(f"    {line}")
    finally:
        try:
            shutil.rmtree(run)
        except OSError as exc:
            print(f"install-agreement: {run} could not be removed ({exc.strerror})", file=sys.stderr)
    # The last stdout line is the contract heading-install.py --dry-run reads (AGREEMENT_CLEAN there).
    print("install-agreement: CLEAN, 0 findings" if not total else f"install-agreement: {total} findings")
    return 0 if not total else 1


if __name__ == "__main__":
    raise SystemExit(main())
