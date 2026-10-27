"""GUARANTEE: the test system can only get smaller, and what a dropped test knew is not lost.

ONE GUARANTEE, FIVE GATES, AND THE REASON THEY ARE IN ONE FILE. The suite's size
has two directions and each needs its own mechanism, but they are the same
promise: what was parked stays parked and stays counted
(`config/parked-tests.json`, which may only fall), and what is live cannot breed
(`config/test-budget.json`, three ceilings which may only fall). Splitting them
across two files was the first arrangement and the operator refused it on the
right ground: a ceiling that goes down and a count that goes down are not two
subjects, and one file per GUARANTEE is the rule this whole rework rests on.

The third part is what makes the other two affordable. The debugging default
inverted on 2026-09-21: a fix ships with no regression test unless the defect
belongs to failure A or failure B. A regression test is not only a gate, it is
also the RECORD of what was understood at the moment of the repair, and that is
the part nobody can reconstruct later. So the record stays and stops weighing
four hundred lines of pytest: `scripts/check-fix-note.py` requires one line in
the commit message. The operator's words, translated: "Knowledge is not lost!"

A ceiling without the inverted default becomes a standing negotiation to raise
the ceiling. An inverted default without the ceiling holds until the first night
somebody decides their case is special. Either one without the Fix-Note line
loses the understanding rather than the pytest. None of the three works alone,
which is why one file holds all of them.

WHY THE PARKED HALF BELONGS TO FAILURE A, and it is the unusual shape of it: not
one private byte, but disclosure by accumulation. A large test corpus records,
file name by file name and docstring by docstring, the layout of the tree it
runs against and what the machine it runs on is wired to. No single line of it
is a credential and no gate would have refused any of it, which is exactly why
nothing caught it. The measurement that established this was taken once,
reported to the operator, and is deliberately NOT written here: a count of how
many parked files named which private directory is the same disclosure in
shorter form.

THE OPERATOR'S RULE ON THE MANIFEST, in his words (translated), on being shown a
first draft that carried the parked directory and every filename: **"nothing in
the system should know where the old tests are, or something could go and run
them by itself, and we do not want that."** He struck both on sight. A path in a manifest
is not documentation, it is an instruction something eventually follows. A
number is not. So the assertion below is written MECHANICALLY rather than as a
reading: it refuses the SHAPE of an address, and a future convenience that
restores one goes red without anyone having to notice.

THIS FILE SITS INSIDE THE CORPUS IT MEASURES, and that is deliberate rather than
overlooked. Every line added here raises the number it guards, so the guard pays
rent like everything else and gets no exemption. The operator raised the point;
the answer is that an exemption is how a budget starts having exceptions.

HOW IT IS DRIVEN. The budget half builds a throwaway repository under the test's
own `tmp_path`, copies the REAL gate scripts into it byte for byte, and runs
them there through `subprocess` exactly as pre-commit does. Both scripts derive
the tree they inspect from their own location, so a copy in a fixture inspects
the fixture. The alternative, a `--root` flag, was rejected: a flag that
re-points a guard at another tree is a bypass shipped with the guard. The
fixture repository makes commits; those are `git commit` calls inside
`tmp_path`, in a repository this test created and deletes, and they are the only
way to give the raise gate a HEAD to compare against. They release nothing. The
parked half reads `config/parked-tests.json` as it sits on disk and drives
`scripts/utils/test_tree.py`, the only module in this repository that reads it;
there is no gate script in front of that one, by design, because the cheapest
way not to have an input that can arrive empty is not to have the input.

NOT CLAIMED. Nothing here says whether the ceilings are set at the right
numbers, whether the tests underneath them are any good, or whether a raise was
warranted; `scripts/check-test-budget-raise.py`'s own docstring states that it
establishes visibility in git history and not authorisation. Nothing here
locates, counts or verifies a parked test, and must never be changed to. The
parked ratchet is held against the number measured on 2026-09-21 rather than
against git history, so lowering it is an edit a person makes and raising it is
what goes red. The address check is a SHAPE test: a string naming nothing
recognisable as a path passes it, and so would an address written as prose in
the explanatory block, which is a carve-out narrowed but not closed below.
"""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils import test_tree  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BUDGET = "scripts/check-test-budget.py"
RAISE = "scripts/check-test-budget-raise.py"
FIXNOTE = "scripts/check-fix-note.py"
COPIED = (BUDGET, RAISE, FIXNOTE, "scripts/utils/test_tree.py",
          "scripts/utils/__init__.py")

# MEASURED 2026-09-21: the gate owns three ceilings. The number is floored here,
# OUTSIDE every loop, because the loops below iterate over the gate's own list
# and a list that came back empty would pass every one of them while checking
# nothing.
CEILING_FLOOR = 3


def _gate_module():
    """The real gate, imported, so its ceiling names are READ rather than copied.

    Obligation 5 of the HOS-54 brief: ask the gate, do not tell it. A copy of
    the three key names here would drift the day a fourth dimension is added,
    and this file would keep passing over the three it knew.
    """
    path = ROOT / BUDGET
    if not path.is_file():
        pytest.fail(f"{BUDGET} does not exist, so the ceiling this file is "
                    f"supposed to hold is not in this repository at all")
    spec = importlib.util.spec_from_file_location("_hos54_budget", path)
    module = importlib.util.module_from_spec(spec)
    # The gate's module body inserts `scripts/` into `sys.path` so it can reach
    # `utils.test_tree`, and this suite arms a guard that fails any test leaving
    # `sys.path` changed. Snapshot and assign back, never `remove()` one string:
    # the module may insert a directory that was already there and two copies go
    # on, so removing one leaves the other behind.
    saved = sys.path[:]
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = saved
    return module


@pytest.fixture(scope="module")
def ceilings() -> tuple[str, ...]:
    names = _gate_module().CEILINGS
    assert len(names) >= CEILING_FLOOR, (
        f"the gate declares {len(names)} ceilings, below the {CEILING_FLOOR} "
        f"measured 2026-09-21. A dimension that stopped being capped is where "
        f"the growth goes next, which is the whole reason there are three.")
    return tuple(names)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True,
                   capture_output=True, text=True, timeout=120)


def _fixture(tmp_path: Path) -> Path:
    """A repository holding the real scripts and two small test files."""
    repo = tmp_path / "fixture"
    for rel in COPIED:
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        source = ROOT / rel
        if not source.is_file():
            pytest.fail(f"{rel} is missing from this repository, so the gate "
                        f"cannot be driven at all")
        shutil.copyfile(source, dest)
    tests = repo / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / "test_alpha.py").write_text(
        "def test_one():\n    assert True\n", encoding="utf-8")
    (tests / "test_beta.py").write_text(
        "def test_two():\n    assert True\n\n\ndef test_three():\n    assert True\n",
        encoding="utf-8")
    (repo / "config").mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", ".")
    _git(repo, "config", "user.email", "zzqx@example.invalid")
    _git(repo, "config", "user.name", "ZZQX")
    _run(repo, BUDGET, "--seed")
    _git(repo, "add", "-A")
    # No `--no-verify`, which this workspace forbids outright: the fixture is a
    # bare `git init` with no hooks installed, so there is nothing to bypass.
    _git(repo, "commit", "-q", "-m", "ZZQX fixture baseline")
    return repo


def _run(repo: Path, script: str, *args: str) -> subprocess.CompletedProcess:
    """The real entry point, in the fixture, exactly as pre-commit invokes it."""
    return subprocess.run([sys.executable, str(repo / script), *args],
                          cwd=str(repo), capture_output=True, text=True,
                          timeout=300)


def _budget(repo: Path) -> dict:
    return json.loads((repo / "config/test-budget.json").read_text(encoding="utf-8"))


def test_a_tree_at_its_ceiling_passes(tmp_path):
    """The honest direction, and without it the rest proves nothing.

    A gate that refused every tree would satisfy every refusal case below and
    would block every commit in the repository.
    """
    repo = _fixture(tmp_path)
    result = _run(repo, BUDGET, "--check")
    assert result.returncode == 0, (
        f"the gate refused a tree that has not moved:\n{result.stderr}")


def test_every_ceiling_the_gate_declares_actually_refuses(ceilings, tmp_path):
    """One case per dimension, read from the gate rather than listed here.

    Each dimension is lowered by one below the measured truth in turn, with the
    others left where they are, so the refusal names that dimension and no
    other. Lowering the ceiling rather than growing the tree is what isolates
    them: adding a test file moves all three at once.
    """
    repo = _fixture(tmp_path)
    unenforced: list[str] = []
    for key in ceilings:
        budget = _budget(repo)
        budget[key] -= 1
        (repo / "config/test-budget.json").write_text(
            json.dumps(budget, indent=2) + "\n", encoding="utf-8")
        result = _run(repo, BUDGET, "--check")
        if result.returncode == 0 or key not in result.stderr:
            unenforced.append(key)
    assert not unenforced, (
        "the gate declares these ceilings and does not refuse when the tree is "
        "over them: " + ", ".join(unenforced))


def test_the_ceiling_comes_down_on_its_own_and_asks_for_a_restage(tmp_path):
    """Lowering is free, automatic, and visible. The direction in one case."""
    repo = _fixture(tmp_path)
    before = _budget(repo)
    (repo / "tests/test_beta.py").unlink()

    first = _run(repo, BUDGET, "--check")
    assert first.returncode == 1, (
        "the gate passed over a tree smaller than its ceiling instead of "
        "bringing the ceiling down, so the ceiling would have stayed at the "
        "old number and left room to grow back into")
    after = _budget(repo)
    assert all(after[k] <= before[k] for k in before if isinstance(before[k], int)), (
        f"a ceiling ROSE while the tree shrank: {before} -> {after}")
    assert after != before, "the gate reported a lowering and wrote nothing"

    second = _run(repo, BUDGET, "--check")
    assert second.returncode == 0, (
        f"the gate refused the tree it had just written the ceiling for:\n"
        f"{second.stderr}")


def test_an_absent_ceiling_file_is_red_and_never_green(tmp_path):
    """An input that is missing REFUSES.

    Obligation 6 of the HOS-54 brief, and it is not theoretical here:
    `scripts/content-guard.py`'s own docstring records a denylist arriving
    empty on a clone with no overlay, the gate exiting 0 printing "denylist
    unavailable; skipped", and all 92 tests over it passing.
    """
    repo = _fixture(tmp_path)
    (repo / "config/test-budget.json").unlink()
    result = _run(repo, BUDGET, "--check")
    assert result.returncode != 0, (
        "the gate exited 0 with no ceiling file present, which is a gate "
        "checking nothing and reporting success")


def test_a_ceiling_that_is_present_but_empty_is_red_too(tmp_path):
    """The other half of the same failure: a file that exists and says nothing."""
    repo = _fixture(tmp_path)
    (repo / "config/test-budget.json").write_text("{}\n", encoding="utf-8")
    result = _run(repo, BUDGET, "--check")
    assert result.returncode != 0, (
        "the gate treated a ceiling file with no ceilings in it as a pass; an "
        "absent ceiling is not a ceiling at zero, it is no gate at all")


def test_a_raise_without_the_declaration_is_refused(ceilings, tmp_path):
    """The raise half, driven through its own entry point with a message file."""
    repo = _fixture(tmp_path)
    budget = _budget(repo)
    budget[ceilings[0]] += 40
    (repo / "config/test-budget.json").write_text(
        json.dumps(budget, indent=2) + "\n", encoding="utf-8")
    message = repo / "msg.txt"
    message.write_text("feat: ZZQX more tests\n\nbody\n", encoding="utf-8")

    refused = _run(repo, RAISE, str(message))
    assert refused.returncode != 0, (
        "a ceiling rose by 40 and the gate let the message through without "
        "the raise appearing anywhere in it")
    assert ceilings[0] in refused.stderr

    message.write_text(
        "feat: ZZQX more tests\n\nTest-Budget-Raise: ZZQX reason\n",
        encoding="utf-8")
    allowed = _run(repo, RAISE, str(message))
    assert allowed.returncode == 0, (
        f"the declaration was present and the gate still refused:\n"
        f"{allowed.stderr}")


def test_a_lowering_is_never_asked_to_declare_itself(ceilings, tmp_path):
    """Shrinking costs nothing. The direction, asserted rather than assumed."""
    repo = _fixture(tmp_path)
    budget = _budget(repo)
    for key in ceilings:
        budget[key] = max(budget[key] - 1, 0)
    (repo / "config/test-budget.json").write_text(
        json.dumps(budget, indent=2) + "\n", encoding="utf-8")
    message = repo / "msg.txt"
    message.write_text("chore: ZZQX fewer tests\n", encoding="utf-8")
    result = _run(repo, RAISE, str(message))
    assert result.returncode == 0, (
        f"the gate demanded a declaration for a LOWERING, which would make "
        f"shrinking the suite more expensive than growing it:\n{result.stderr}")


def _stage_a_code_change(repo: Path) -> None:
    """A staged edit under `scripts/`, which is what the Fix-Note gate fires on."""
    (repo / "scripts/zzqx_thing.py").write_text(
        "VALUE = 'ZZQX'\n", encoding="utf-8")
    _git(repo, "add", "scripts/zzqx_thing.py")


def _message(repo: Path, text: str) -> Path:
    path = repo / "msg.txt"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_fix_with_no_test_and_no_note_is_refused(tmp_path):
    """The second half of the pair, and the reason the first half is affordable.

    The debugging default inverted on 2026-09-21: a fix ships with no
    regression test unless the defect belongs to failure A or failure B. What a
    regression test also carried, and what nobody can reconstruct afterwards, is
    the understanding at the moment of the repair. One line keeps it. Operator
    decision the same day, translated: "Knowledge is not lost!"
    """
    repo = _fixture(tmp_path)
    _stage_a_code_change(repo)
    result = _run(repo, FIXNOTE, str(_message(repo, "fix: ZZQX something\n")))
    assert result.returncode != 0, (
        "a fix to code shipped with neither a test nor the sentence that "
        "replaces one, and the gate said nothing")


def test_the_note_is_accepted_and_so_is_a_real_test(tmp_path):
    """Both ways out, because a gate with only one is a gate that gets removed."""
    repo = _fixture(tmp_path)
    _stage_a_code_change(repo)
    noted = _run(repo, FIXNOTE, str(_message(
        repo, "fix: ZZQX something\n\nFix-Note: a | b | c\n")))
    assert noted.returncode == 0, (
        f"the note was present and the gate still refused:\n{noted.stderr}")

    (repo / "tests/test_gamma.py").write_text(
        "def test_zzqx_regression():\n    assert True\n", encoding="utf-8")
    _git(repo, "add", "tests/test_gamma.py")
    tested = _run(repo, FIXNOTE, str(_message(repo, "fix: ZZQX something\n")))
    assert tested.returncode == 0, (
        f"a fix that DID add a test function was asked for a note as well:\n"
        f"{tested.stderr}")


def test_a_commit_that_is_not_a_fix_is_not_asked_for_a_note(tmp_path):
    """The honest direction, and it is also the gate's stated limit.

    A fix labelled `chore:` is not asked for a note. That is not an oversight
    to be closed by widening the trigger to every commit touching `scripts/`: a
    required field with nothing to say in it gets filled with noise and then
    stops being read. The limit is asserted here so a later widening is a
    decision somebody makes rather than a drift nobody notices.
    """
    repo = _fixture(tmp_path)
    _stage_a_code_change(repo)
    for subject in ("chore: ZZQX housekeeping\n", "feat: ZZQX a new thing\n",
                    "merge: ZZQX a branch\n"):
        result = _run(repo, FIXNOTE, str(_message(repo, subject)))
        assert result.returncode == 0, (
            f"a {subject.split(':')[0]} commit was asked for a Fix-Note:\n"
            f"{result.stderr}")


# ---------------------------------------------------------------------------
# The parked half: what left stays gone, stays counted, and never grows an address.
# ---------------------------------------------------------------------------

MANIFEST = ROOT / test_tree.MANIFEST_REL

# MEASURED 2026-09-21 from config/parked-tests.json: 1378 test files and 93
# support files parked, and the record holds 36 strings across its keys and
# values. The counts are CEILINGS, because the manifest's own `direction` field
# says the number may only fall. The string count is a FLOOR under the recursive
# walk below, asserted outside every loop, because a walk that returned nothing
# would pass every address check while reading nothing.
PARKED_TEST_FILES_CEILING = 1378
PARKED_SUPPORT_FILES_CEILING = 93
STRINGS_FLOOR = 36

# The key holding the explanatory prose. It is the one place a `/` may legally
# appear, and naming it here is what keeps the carve-out visible instead of
# implicit.
PROSE_KEY = "_what_this_is"

# A python module filename, anywhere in a string. Every one of the 1471 parked
# entries had this shape, so it is the file-list shape itself.
_FILENAME = re.compile(r"[\w\-]+\.pyc?\b")
# An absolute address, in any of the three spellings this machine can produce.
_ABSOLUTE = re.compile(r"^(?:/|~|[A-Za-z]:[\\/])")
# The private overlay, named. Not a path check: a bare repository name is enough
# to say where to look.
_OVERLAY = re.compile(r"heading-os-data|\.\./", re.IGNORECASE)


def _strings(value, key: str | None = None):
    """Every string in the record, with the top-level key it descends from."""
    if isinstance(value, str):
        yield key, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield None, k
            yield from _strings(v, key if key is not None else k)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v, key)


def _address_shaped(text: str) -> str | None:
    """Why this string is an address, or None. The mechanical form of the rule."""
    if _ABSOLUTE.search(text):
        return "absolute"
    if _FILENAME.search(text):
        return "names a python file"
    if _OVERLAY.search(text):
        return "names the private overlay"
    return None


@pytest.fixture(scope="module")
def manifest() -> dict:
    """The record, or RED. An absent manifest is never a skip and never green."""
    try:
        loaded = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except OSError as exc:
        pytest.fail(f"{MANIFEST} could not be read, so the parked budget is "
                    f"unknown and nothing below has been checked: {exc}")
    except ValueError as exc:
        pytest.fail(f"{MANIFEST} is not valid JSON, so every reader of it "
                    f"silently falls back to 0: {exc}")
    if not isinstance(loaded, dict):
        pytest.fail(f"{MANIFEST} does not hold an object; the record's shape "
                    f"has changed and this file is now blind to it")
    return loaded


def test_the_manifest_parses_and_its_counts_are_integers(manifest):
    """`parked_test_files` is an integer, asked by type and not by truthiness.

    `type(...) is int` rather than `isinstance`, because `True` is an instance of
    `int` in Python and a boolean in this field would read as 1 parked file to
    every caller. A string here reads as 0 through the reader's own
    `except (OSError, ValueError, TypeError)` and reports the parked suite as
    empty.
    """
    for field, ceiling in (("parked_test_files", PARKED_TEST_FILES_CEILING),
                           ("parked_support_files", PARKED_SUPPORT_FILES_CEILING)):
        assert field in manifest, f"{MANIFEST} carries no {field!r}"
        value = manifest[field]
        assert type(value) is int, (  # noqa: E721
            f"{field} is {type(value).__name__} ({value!r}), not an int, so "
            f"every reader of it falls back to 0 and reports nothing parked")
        assert value >= 0, f"{field} is negative: {value}"
        assert value <= ceiling, (
            f"{field} rose to {value} from the {ceiling} measured 2026-09-21. "
            f"The manifest's own `direction` field says it may only fall; a "
            f"rise is the operator's to authorise, not a test's to accept")


def test_the_reader_and_the_record_cannot_disagree(manifest):
    """`parked_count()` is the only thing in this repository that reads the file.

    Driven through the shipped function rather than re-implemented here, so a
    change to how it reads the record is followed rather than diverged from. Its
    `except` clause answers 0 for an unreadable manifest, which is the same
    answer as an empty one, so this is where those two are told apart.
    """
    assert test_tree.parked_count(ROOT) == manifest["parked_test_files"], (
        f"scripts/utils/test_tree.parked_count() reads "
        f"{test_tree.parked_count(ROOT)} where the record says "
        f"{manifest['parked_test_files']}; one of the two is being read wrong")


def test_the_manifest_holds_no_address_and_no_container_that_could_become_one(manifest):
    """The operator's rule, written as a shape rather than as a reading.

    Two halves. Every string is checked for the address shapes, including the
    prose block, which is allowed a bare `/` and nothing more. And every value
    except that block must be a SCALAR, which is what refuses the file-list shape
    structurally: a list of 1471 filenames cannot return to a record that holds
    no lists.
    """
    strings = list(_strings(manifest))
    # The floor, outside the loop.
    assert len(strings) >= STRINGS_FLOOR, (
        f"the walk found {len(strings)} strings in {MANIFEST}, below the "
        f"{STRINGS_FLOOR} measured 2026-09-21; it is reading less of the record "
        f"than the record holds and the checks below prove nothing")

    addresses = [f"{key or '<key>'}: {text!r} ({why})"
                 for key, text in strings
                 if (why := _address_shaped(text))]
    assert not addresses, (
        "the parked manifest has grown something shaped like an address, which "
        "is the one thing the operator struck from it on sight: "
        + "; ".join(addresses[:5]))

    containers = sorted(k for k, v in manifest.items()
                        if k != PROSE_KEY and isinstance(v, (list, dict)))
    assert not containers, (
        f"{MANIFEST} has grown a container outside {PROSE_KEY!r}: {containers}. "
        f"A record that holds only scalars cannot hold a file list")

    # The prose carve-out is allowed a `/`; no other value is.
    slashed = sorted(k for k, v in manifest.items()
                     if k != PROSE_KEY and isinstance(v, str) and "/" in v)
    assert not slashed, (
        f"value(s) outside {PROSE_KEY!r} carry a path separator: {slashed}")


def test_the_address_check_actually_refuses_the_shapes_it_names():
    """The refusal direction, and without it the case above proves nothing.

    Every planted string is one the manifest used to carry or could plausibly
    acquire, and each must be reported. The honest half is last: a string that
    names no address passes, so the predicate is not simply refusing everything.
    """
    planted = [
        "tests/parked/test_zzqx_thing.py",
        "test_zzqx_other.py",
        # NOT written under `/home/`, and the reason is a gate rather than
        # taste: `scripts/check-home-paths.py` refuses that shape anywhere in
        # this public repository, invented account name or not, because a home
        # path publishes the account, the machine's layout and the names of its
        # working trees. It refused this line on its first commit. The fixture
        # only needs an ABSOLUTE path to exercise the check, so it gets one
        # that carries no account name at all. No allowlist entry was added.
        "/zzqx/somewhere/tests",
        "~/zzqx/tests",
        "../.heading-os-data/zzqx",
        "the ZZQX overlay at heading-os-data",
    ]
    survivors = [text for text in planted if _address_shaped(text) is None]
    assert not survivors, (
        f"the address check admitted {len(survivors)} planted address(es), so "
        f"the case above would admit them too: {survivors}")

    honest = ["2026-09-21",
              "may only fall; a rise is refused and is the operator's to authorise",
              "A COUNT, and deliberately nothing else."]
    refused = [text for text in honest if _address_shaped(text) is not None]
    assert not refused, (
        f"the address check refused ordinary prose, so it refuses everything "
        f"and the case above is satisfied by nothing: {refused}")
