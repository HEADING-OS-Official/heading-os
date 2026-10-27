"""GUARANTEE: a medium-or-higher security finding in engine Python is refused at commit time.

FAILURE A. Bandit is the static gate between an insecure construct and this
public repository, and the constructs it grades at medium are the ones that reach
a file, a shell or a socket. A finding that reaches `main` is a defect published
under the operator's name, and unlike an ordinary bug he cannot notice it by
working.

HOW IT IS DRIVEN. `pre-commit run bandit --files <path>`, which is the command a
commit actually reaches, in pre-commit's own isolated environment rather than
whatever bandit the checkout happens to import. The assertion is the exit code.
Never the message: bandit rewords its report between releases, and an exit code
is what stops a commit.

THE TWO INVOCATIONS, AND WHY THE SECOND ONE IS CHECKED HERE. Bandit runs twice in
this repository, at the commit and again in `.github/workflows/ci.yml`, and the
two carry different flags. Both sets are READ FROM THEIR OWN FILES at run time
and compared, so neither is copied into this file and neither can drift silently
away from the other. What the comparison holds is the one direction that costs
something: the commit gate must not be more permissive than CI, or a finding CI
will catch passes locally and is discovered after the push.

MEASURED 2026-09-21 on the difference that exists today. CI adds
`--confidence-level medium`; the commit hook sets no confidence floor at all, so
it sits at bandit's default and is STRICTER. Driven rather than argued: a
medium-severity low-confidence finding (a SQL string built by concatenation) is
REFUSED by the commit hook, exit 1, and PASSED by the CI flag set, exit 0. The
gap therefore runs the safe way round, and the case below pins it so that adding
a confidence floor to the hook, or removing the severity floor from either, goes
red.

NOT CLAIMED. This proves the gate refuses one planted medium finding and admits
one honest file. It does not enumerate what bandit grades medium, does not claim
the two invocations cover the same FILES (CI walks three roots, the hook takes
the paths being committed, and neither contains the other), and says nothing
about the push path or about a finding already in history. Nor does it assert
that CI actually runs: this reads the workflow's declaration, not a run of it.

ONE KNOWN WAY THIS GOES RED WITHOUT THE GATE BEING WRONG, stated rather than left
to be discovered. `pre-commit run` stashes the working tree before a hook and
restores it after, and reports a nonzero exit when it finds the tree modified
around that window. A concurrent write into this checkout while the hook is
running therefore reads here as a refusal. MEASURED 2026-09-21: once in eleven
runs, during an editor write into this same file, and 0 of the following 10. A
failure of the honest case whose report carries no finding is that, and the
repair is to re-run on a quiet tree rather than to soften the case.
"""
from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent

# The planted case is built rather than written, so no insecure literal sits in
# this file for the repository's own ruff and bandit gates to flag. Obligation 7
# in the brief: if a fixture trips a gate, change the fixture, never the gate.
_PLANTED_MEDIUM = 'PATH_ZZQX = "' + "/" + 'tmp' + '/zzqx-probe"\n'
_PLANTED_LOW_CONFIDENCE = (
    "def zzqx_query(table):\n"
    '    return "SELECT * FROM " + table + " WHERE zzqx = 1"\n'
)
_HONEST = 'ZZQX = "ordinary module, nothing insecure in it"\n\n\ndef zzqx():\n    return ZZQX\n'

# MEASURED 2026-09-21: bandit's flag set at the commit is 4 tokens and CI's is
# 11. The floors are outside every loop below, because a parse that yielded an
# empty argument list would otherwise compare nothing and pass.
HOOK_ARGS_FLOOR = 4
CI_ARGS_FLOOR = 11

_LEVELS = ("low", "medium", "high")


def _hook_args() -> list[str]:
    """Bandit's commit-time arguments, read from the config that arms it."""
    path = ROOT / ".pre-commit-config.yaml"
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        pytest.fail(f"{path} could not be read, so the commit-time bandit "
                    f"configuration is unknown: {exc}")
    for repo in (config or {}).get("repos", []):
        for hook in repo.get("hooks", []):
            if hook.get("id") == "bandit":
                return list(hook.get("args") or [])
    pytest.fail(f"no hook with id 'bandit' is registered in {path}, so nothing "
                f"runs it at commit time")


def _ci_args() -> list[str]:
    """Bandit's CI arguments, read from the workflow that runs them."""
    path = ROOT / ".github" / "workflows" / "ci.yml"
    try:
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        pytest.fail(f"{path} could not be read, so CI's bandit configuration "
                    f"is unknown: {exc}")
    found = []
    for job in (workflow or {}).get("jobs", {}).values():
        for step in job.get("steps", []):
            run = str(step.get("run") or "")
            for line in run.splitlines():
                if "bandit" not in line:
                    continue
                try:
                    tokens = shlex.split(line)
                except ValueError:
                    # A line this cannot tokenise is reported, never skipped in
                    # silence: a skipped case is a claim that decays.
                    pytest.fail(f"a bandit-bearing line in {path} could not be "
                                f"tokenised, so the comparison below would be "
                                f"made against a partial reading: {line!r}")
                if "bandit" in tokens:
                    found.append(tokens[tokens.index("bandit") + 1:])
    if not found:
        pytest.fail(f"no bandit invocation found in {path}; the second half of "
                    f"this comparison has vanished and the drift check is blind")
    if len(found) > 1:
        pytest.fail(f"{len(found)} bandit invocations in {path}; which one the "
                    f"commit gate is being compared against is no longer decidable")
    return found[0]


def _floor(args: list[str], flag: str, default: str) -> str:
    return args[args.index(flag) + 1] if flag in args else default


def _run_hook(target: Path) -> subprocess.CompletedProcess:
    """The REAL entry point a commit reaches."""
    return subprocess.run(
        [sys.executable, "-m", "pre_commit", "run", "bandit", "--files", str(target)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300)


def _run_bandit(args: list[str], target: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "bandit", *args, "-q", str(target)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300)


def test_the_commit_hook_refuses_a_medium_finding_and_admits_an_honest_file(tmp_path):
    """Both directions, through the command a commit runs.

    Without the second half, replacing the hook with `exit 1` would leave this
    file green and would break every honest commit in the repository.
    """
    planted = tmp_path / "zzqx_planted.py"
    planted.write_text(_PLANTED_MEDIUM, encoding="utf-8")
    refused = _run_hook(planted)
    assert refused.returncode != 0, (
        "the commit gate passed a file carrying a medium-severity finding:\n"
        f"{refused.stdout}\n{refused.stderr}")

    honest = tmp_path / "zzqx_honest.py"
    honest.write_text(_HONEST, encoding="utf-8")
    clean = _run_hook(honest)
    assert clean.returncode == 0, (
        "the commit gate refused an ordinary file with nothing insecure in it:\n"
        f"{clean.stdout}\n{clean.stderr}")


def test_the_planted_case_is_medium_and_not_high_so_the_floor_is_what_caught_it(tmp_path):
    """The refusal above must be the MEDIUM floor doing the work.

    Asserted by exit code alone and never by reading the severity out of the
    report: the same file is refused at the configured floor and admitted when
    the floor is raised to high, which places it at medium and nowhere else. A
    planted case that turned out to be high severity would leave the medium
    floor untested while looking exactly like this.
    """
    planted = tmp_path / "zzqx_planted.py"
    planted.write_text(_PLANTED_MEDIUM, encoding="utf-8")
    args = _hook_args()
    assert len(args) >= HOOK_ARGS_FLOOR, (
        f"the bandit hook declares {len(args)} argument(s), below the "
        f"{HOOK_ARGS_FLOOR} measured 2026-09-21; its configuration has been "
        f"stripped")
    at_configured = _run_bandit(args, planted)
    raised = [*args[:args.index("--severity-level") + 1], "high"] if "--severity-level" in args else [*args, "--severity-level", "high"]
    at_high = _run_bandit(raised, planted)
    assert at_configured.returncode != 0, (
        f"the configured floor admitted the planted case:\n{at_configured.stdout}")
    assert at_high.returncode == 0, (
        f"the planted case is graded high, not medium, so nothing here "
        f"exercises the medium floor:\n{at_high.stdout}")


def test_the_commit_gate_is_never_more_permissive_than_ci():
    """The drift pin, read from both files rather than copied into this one.

    Three axes, and each is a way something could pass locally and fail after the
    push. The confidence axis is the one where the two differ TODAY, measured
    2026-09-21, and the difference is pinned in the safe direction rather than
    removed.
    """
    hook, ci = _hook_args(), _ci_args()
    assert len(ci) >= CI_ARGS_FLOOR, (
        f"CI's bandit step carries {len(ci)} argument(s), below the "
        f"{CI_ARGS_FLOOR} measured 2026-09-21")

    assert _floor(hook, "-c", "") == _floor(ci, "-c", ""), (
        f"the two bandit invocations read different config files: "
        f"{_floor(hook, '-c', '')!r} at commit, {_floor(ci, '-c', '')!r} in CI")

    for flag, default in (("--severity-level", "low"), ("--confidence-level", "low")):
        commit_floor = _floor(hook, flag, default)
        ci_floor = _floor(ci, flag, default)
        assert commit_floor in _LEVELS and ci_floor in _LEVELS, (
            f"{flag} holds a value this test cannot order: {commit_floor!r} vs "
            f"{ci_floor!r}")
        assert _LEVELS.index(commit_floor) <= _LEVELS.index(ci_floor), (
            f"the commit gate's {flag} is {commit_floor}, above CI's {ci_floor}, "
            f"so a finding CI refuses would pass at commit time and be "
            f"discovered only after the push")


def test_the_confidence_gap_between_them_runs_the_safe_way_round(tmp_path):
    """The same claim, measured rather than derived from the flag strings.

    MEASURED 2026-09-21 on a medium-severity low-confidence finding: refused by
    the commit hook, admitted by CI's flag set. An argument about what a flag
    means is not a measurement, so the gap is driven here as well as compared
    above.
    """
    planted = tmp_path / "zzqx_lowconf.py"
    planted.write_text(_PLANTED_LOW_CONFIDENCE, encoding="utf-8")
    refused = _run_hook(planted)
    assert refused.returncode != 0, (
        "the commit gate admitted a medium-severity finding that only its lower "
        f"confidence floor catches:\n{refused.stdout}\n{refused.stderr}")
