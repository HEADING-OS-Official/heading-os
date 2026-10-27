"""GUARANTEE: the Yelp detect-secrets gate is armed with a baseline and refuses a planted high-entropy credential.

FAILURE A: a private byte reaching this public repository. This gate is the
entropy-based half of the commit wall, complementary to the pattern-based
`secret-scanner-workspace` hook that runs beside it; neither covers the other.

HOW IT IS DRIVEN. `pre-commit run detect-secrets --files <f>`, in a subprocess,
which is the command a commit reaches. The assertion is the EXIT CODE, never the
wording, because the message is prose the hook may reword and the exit code is
what stops a commit.

THE LIST IS READ FROM THE GATE, NEVER COPIED. The plugin roster comes from
`.secrets.baseline`, which is what `--baseline` hands the gate at run time, and
each name is then required to resolve to a class in the INSTALLED copy of
`detect_secrets`. The installed copy matters and the lesson was paid for: a
hook repository holds its sources at its root AND an installed copy inside each
`py_env-*`, and `pre-commit run` executes the installed one. Reading the wrong
one made a sibling gate test green against a gate mutated to refuse nothing. The
class names are taken with `ast`, not with a substring scan, so a name that
appears only in a comment or a docstring is not evidence.

THE EXCLUSION LIST IS PINNED FROM ABOVE. Each alternative in the hook's
`exclude` block is a place this gate stops looking, so the count carries a
CEILING rather than a floor: the list may shrink freely and may not grow without
someone changing the measured number and saying why. The reason behind each
alternative is written out in the config beside it and is deliberately not
restated here.

NOT CLAIMED. This says nothing about what the gate does to a file the exclusion
block selects, nothing about the pattern-based scanner running beside it,
nothing about the push-time wall, and nothing about a secret already in history.
It establishes that the hook is registered and that it refuses and permits
correctly; whether every entropy plugin in the roster would catch its own shape
of credential is not asserted. No fixture value was added to any allowlist or
to the baseline: the planted credential is derived at run time, so no
credential-shaped literal exists in this file.
"""
from __future__ import annotations

import ast
import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
HOOK_ID = "detect-secrets"

# MEASURED 2026-09-21 against Yelp/detect-secrets v1.5.0 as pinned in
# .pre-commit-config.yaml; the exclusion ceiling lowered to 1 on 2026-10-16,
# when the four `outputs/` alternatives that could no longer match and the
# audit-rotation ledger (moved to the data repository) left. Floors and ceilings sit OUTSIDE every loop, because a
# loop over a roster that came back empty passes while checking nothing.
PLUGIN_FLOOR = 27          # plugins_used in .secrets.baseline
EXCLUSION_CEILING = 1      # alternatives in the hook's exclude block


def _planted_value() -> str:
    """A deterministic 44-character high-entropy string, derived not written.

    Not a literal. This workspace's own PreToolUse wall refuses the writing of a
    credential-shaped literal, and it is right to: obligation 7 says change the
    fixture, never teach a gate that one particular thing looks private but is
    fine.
    """
    return base64.b64encode(
        hashlib.sha256(b"ZZQX-hos54-detect-secrets-fixture").digest()).decode()


@pytest.fixture(scope="module")
def hook() -> dict:
    """The detect-secrets stanza, read from the config that arms it."""
    config = ROOT / ".pre-commit-config.yaml"
    if not config.is_file():
        pytest.fail(f"no {config}, so nothing arms this gate at commit time")
    parsed = yaml.safe_load(config.read_text(encoding="utf-8"))
    found = [h for repo in parsed.get("repos", [])
             for h in repo.get("hooks", []) if h.get("id") == HOOK_ID]
    if len(found) != 1:
        pytest.fail(
            f"{HOOK_ID} appears {len(found)} times in {config.name}; a gate that "
            f"is absent runs on no commit, and two stanzas mean the one driven "
            f"below may not be the one a commit reaches")
    return found[0]


@pytest.fixture(scope="module")
def baseline(hook) -> dict:
    """The gate's own roster, loaded from the file `--baseline` names."""
    args = hook.get("args") or []
    if "--baseline" not in args:
        pytest.fail(
            f"the {HOOK_ID} hook carries no --baseline argument, so every run "
            f"re-reports every known finding and the gate becomes noise nobody "
            f"reads. args={args}")
    named = ROOT / args[args.index("--baseline") + 1]
    if not named.is_file():
        pytest.fail(f"--baseline names {named}, which does not exist, so the "
                    f"gate cannot run at all")
    return json.loads(named.read_text(encoding="utf-8"))


def _installed_plugin_classes() -> dict[str, set[str]]:
    """{install path: class names} for every INSTALLED copy of the gate.

    The sources at the hook repository's root are NOT what `pre-commit run`
    executes. Asked of the AST rather than of the text.
    """
    try:
        from pre_commit.store import Store
    except ImportError as exc:  # pragma: no cover - the venv always has it
        pytest.fail(f"pre_commit is not importable, so the installed gate "
                    f"cannot be located: {exc}")
    store = Path(Store().directory)
    copies = sorted({p.parent for p in store.glob(
        "*/py_env-*/lib/*/site-packages/detect_secrets/plugins/__init__.py")})
    if not copies:
        pytest.fail(
            f"{HOOK_ID} is configured but no installed copy of it exists, so the "
            f"gate cannot be driven. Run `pre-commit install-hooks`. "
            f"Searched: {store}")
    out: dict[str, set[str]] = {}
    for copy in copies:
        names: set[str] = set()
        for module in sorted(copy.glob("*.py")):
            tree = ast.parse(module.read_text(encoding="utf-8"), str(module))
            names |= {n.name for n in ast.walk(tree)
                      if isinstance(n, ast.ClassDef)}
        out[str(copy)] = names
    return out


def _run_gate(target: Path) -> subprocess.CompletedProcess:
    """The REAL entry point a commit reaches, not a helper function."""
    return subprocess.run(
        [sys.executable, "-m", "pre_commit", "run", HOOK_ID, "--files", str(target)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=600)


def test_the_gate_refuses_a_planted_high_entropy_credential(tmp_path):
    """The refusal direction, driven through the command a commit runs."""
    target = tmp_path / "planted.py"
    target.write_text(f'TOKEN = "{_planted_value()}"\n', encoding="utf-8")
    result = _run_gate(target)
    assert result.returncode != 0, (
        "the gate passed a file carrying a 44-character high-entropy string:\n"
        f"{result.stdout}\n{result.stderr}")


def test_an_honest_file_still_passes(tmp_path):
    """The other direction, and it is not optional.

    A gate that refuses everything satisfies the case above and blocks every
    commit. Without this, replacing the hook with `exit 1` leaves this file green.
    """
    target = tmp_path / "ordinary.py"
    target.write_text("VALUE = 'ZZQX ordinary content, no key here'\n",
                      encoding="utf-8")
    result = _run_gate(target)
    assert result.returncode == 0, (
        "the gate refused a file with nothing secret in it:\n"
        f"{result.stdout}\n{result.stderr}")


def test_every_plugin_the_baseline_names_exists_in_the_installed_gate(baseline):
    """The roster, read from the gate and looped over, with its floor outside.

    A name in the baseline that the installed package no longer defines is a
    detector that has silently stopped running, which is the shape of weakening
    that leaves every commit green.
    """
    names = sorted(p["name"] for p in baseline.get("plugins_used", []))
    assert len(names) >= PLUGIN_FLOOR, (
        f"the baseline names {len(names)} plugins, below the {PLUGIN_FLOOR} "
        f"measured 2026-09-21. The gate now detects LESS than it did; that is a "
        f"weakening, not a test failure to wave through.")
    installed = _installed_plugin_classes()
    rosters = {frozenset(v) for v in installed.values()}
    assert len(rosters) == 1, (
        "installed copies of the gate disagree about which detectors they "
        "define, so the one a commit reaches may not be the one measured here: "
        + "; ".join(f"{k}={len(v)}" for k, v in installed.items()))
    defined = next(iter(rosters))
    missing = [n for n in names if n not in defined]
    assert not missing, (
        f"{len(missing)} plugin(s) named in the baseline are not defined by the "
        f"installed gate, so they detect nothing: {', '.join(missing)}")


def test_the_places_the_gate_stops_looking_have_not_grown(hook):
    """A ceiling, not a floor, and the asymmetry is the point.

    Every alternative in `exclude` is a path the gate does not read. Shrinking
    the list widens coverage and needs no permission here; growing it narrows
    coverage and must be a deliberate edit to this number with a reason written
    beside the new entry in the config.
    """
    block = hook.get("exclude") or ""
    alternatives = [line.strip().rstrip("|") for line in block.splitlines()
                    if line.strip() and line.strip() not in ("(?x)^(", ")$")]
    assert 0 < len(alternatives) <= EXCLUSION_CEILING, (
        f"the exclusion block holds {len(alternatives)} alternatives against the "
        f"ceiling of {EXCLUSION_CEILING}, each with its reason in the config. "
        f"Each one is a place this gate stops looking.")
