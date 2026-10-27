"""GUARANTEE: a private key committed to this repository is refused.

THE FIRST TEST THIS GATE HAS EVER HAD. `detect-private-key` has run on every
commit here since the pre-commit config was written and nothing has ever
observed it refusing anything. A wall no test names has never been seen to
refuse, which is obligation 11 of the Evidence Standard, and this gate was the
standing exception to it.

WHY THIS FILE CONTAINS NO KEY HEADER, AND WHY THAT IS THE DESIGN RATHER THAN A
WORKAROUND. The cases are not listed here. They are read from the gate's own
blacklist at run time, so this file holds a loop where a hand-written version
would hold a copy. Three things follow, and the third is the one that matters:

  * There is no second list to drift. A header added to the gate tomorrow gets
    a case tomorrow, with nobody editing this file.
  * A header REMOVED from the gate loses its case silently, so the count is
    floored below, with the number that was measured and the date.
  * Nothing in this public repository restates what the gate looks for. A test
    that enumerates a guard's patterns enumerates, by the same act, what is NOT
    among them, and that is an evasion map published beside the guard. The rule
    it obeys is obligation 6: drive the real entry point, assert the observable
    consequence, never restate the guard's own condition inside the test.

An attempt to write this file with one PEM header literal in it was REFUSED by
this workspace's own PreToolUse wall on 2026-09-21. The wall was correct and was
left alone; no allowlist entry was added, because the day a gate learns that a
particular thing looks like a key but is fine, it is dead and it dies quietly.

NOT CLAIMED. This drives the gate through `pre-commit run`, which is how a
commit reaches it. It says nothing about the push path, nothing about a key
already in history, and nothing about a key in a file the hook's own `files:`
pattern does not select.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# MEASURED 2026-09-21 against pre-commit-hooks v4.6.0 as pinned in
# .pre-commit-config.yaml: the gate's blacklist holds 10 headers. The floor is
# the measured number, asserted OUTSIDE every loop, because a loop over a corpus
# that came back empty passes while checking nothing.
BLACKLIST_FLOOR = 10


def _gate_blacklist() -> list[bytes]:
    """The headers the INSTALLED gate refuses, read from the gate itself.

    Not a copy. If this cannot be resolved the caller FAILS rather than skips:
    a test that cannot find the thing it tests has learned nothing, and
    reporting that as a skip is the shape this workspace has already been bitten
    by, written out in scripts/content-guard.py's docstring where an input that
    arrived empty produced exit 0 while 92 tests over that gate passed.
    """
    try:
        from pre_commit.store import Store
    except ImportError as exc:  # pragma: no cover - the venv always has it
        pytest.fail(f"pre_commit is not importable, so the gate cannot be "
                    f"located: {exc}")
    # THE INSTALLED COPY, NOT THE CHECKOUT. A pre-commit hook repository holds
    # the sources at its root AND an installed copy inside each `py_env-*`, and
    # `pre-commit run` executes the INSTALLED one. MEASURED 2026-09-21: reading
    # the root copy instead made this whole file green against a gate mutated to
    # return 0 for every input, which is a gate that refuses nothing. The test
    # was comparing the running gate against a list that was not the running
    # gate's. Found by planting that break rather than by reading the code, and
    # this comment is here so the glob is never "simplified" back.
    found = sorted(Path(Store().directory).glob(
        "*/py_env-*/lib/*/site-packages/pre_commit_hooks/detect_private_key.py"))
    if not found:
        pytest.fail(
            "detect-private-key is configured in .pre-commit-config.yaml but "
            "no installed copy of it exists, so this gate cannot be driven. "
            f"Run `pre-commit install-hooks`. Searched: {Store().directory}")
    lists: dict[str, list[bytes]] = {}
    for module in found:
        namespace: dict = {}
        exec(compile(module.read_text(encoding="utf-8"),  # noqa: S102
                     str(module), "exec"), namespace)
        blacklist = namespace.get("BLACKLIST")
        if not blacklist:
            pytest.fail(f"{module} defines no non-empty BLACKLIST; the gate's "
                        f"shape has changed and this test is now blind to it")
        lists[str(module)] = list(blacklist)
    # Several interpreters, several installs. They must agree, because
    # `pre-commit` picks one by `language_version` and a test that read the
    # other would be measuring a gate the commit never reaches.
    distinct = {tuple(v) for v in lists.values()}
    if len(distinct) != 1:
        pytest.fail("installed copies of the gate disagree about what it "
                    "refuses: " + "; ".join(f"{k}={len(v)} headers"
                                            for k, v in lists.items()))
    return next(iter(lists.values()))


def _run_gate(target: Path) -> subprocess.CompletedProcess:
    """The REAL entry point a commit reaches, not a helper function."""
    return subprocess.run(
        [sys.executable, "-m", "pre_commit", "run", "detect-private-key",
         "--files", str(target)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300,
    )


@pytest.fixture(scope="module")
def blacklist() -> list[bytes]:
    found = _gate_blacklist()
    # The floor, outside every loop. This is the assertion that fails if the
    # corpus collapses, rather than the suite going green over nothing.
    assert len(found) >= BLACKLIST_FLOOR, (
        f"the gate's blacklist holds {len(found)} headers, below the "
        f"{BLACKLIST_FLOOR} measured 2026-09-21. The gate now refuses LESS "
        f"than it did; that is a weakening, not a test failure to wave through."
    )
    return found


def test_the_gate_refuses_every_header_it_claims_to_know(blacklist, tmp_path):
    """Both directions start here: every header planted must be refused.

    One subprocess per header, each driving the real hook. The assertion is the
    EXIT CODE, never the message, because a message is prose the hook may
    reword and an exit code is what stops a commit.
    """
    survivors: list[str] = []
    for header in blacklist:
        target = tmp_path / "planted.pem"
        target.write_bytes(
            b"-----" + header + b"-----\nZZQXnotarealkeyZZQX\n"
            b"-----END-----\n")
        if _run_gate(target).returncode == 0:
            survivors.append(header.decode("utf-8", "replace"))
        target.unlink()
    assert not survivors, (
        "the gate passed a file carrying a header it lists as a private key: "
        + ", ".join(survivors))


def test_an_honest_file_still_passes(tmp_path):
    """The other direction, and it is not optional.

    A guard that refuses everything satisfies every refusal test above and
    breaks every honest caller. Without this, replacing the gate with `exit 1`
    would leave this file green.
    """
    target = tmp_path / "ordinary.py"
    target.write_text("VALUE = 'ZZQX ordinary content, no key here'\n",
                      encoding="utf-8")
    result = _run_gate(target)
    assert result.returncode == 0, (
        "the gate refused a file with no key material in it:\n"
        f"{result.stdout}\n{result.stderr}")


def test_the_gate_is_registered_in_the_config_that_arms_it(blacklist):
    """A gate nothing invokes refuses nothing, whatever its code says.

    The two halves are separate failures and are worth telling apart: the hook
    can be absent from the config, or present and scoped to nothing. This
    asserts only the first; `scripts/check-gate-integrity.py` owns the second
    and already runs on every commit.
    """
    config = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    assert "detect-private-key" in config, (
        "detect-private-key is not registered in .pre-commit-config.yaml, so "
        "nothing runs it at commit time and the cases above prove only that "
        "the hook exists on disk")
