"""GUARANTEE: an engine-routed file cannot carry a real entity or a real name.

FAILURE A, a private byte reaching this public repository. The routing guards
ask WHERE a file goes. This one asks WHAT is inside a file that legitimately
goes here, which is the only layer standing between a real person's name and a
public commit once the file itself routes `engine`.

HOW THIS DRIVES THE GATE. Through `subprocess`, at `scripts/content-guard.py`,
with `--quiet --files`, which is byte for byte the `entry` line
`.pre-commit-config.yaml` registers. One argument is added that the hook does
not pass: `--data-root`, the gate's own documented override. It selects the
overlay the denylist is harvested from, and it is the whole reason this file can
exist in a public repository. The LIVE denylist is built from the operator's
private overlay and every token in it is a real person, a real employer or a
real address, so no test may print one, plant one, or assert on one. This file
builds a THROWAWAY overlay of its own, in `tmp_path`, holding nothing but
invented values carrying the `zzqx` sentinel, and drives the same `main()`
against that. What is NOT therefore established: that the live overlay harvests
correctly on this machine today. This tests the gate, not the operator's data.

THE DEFECT THIS FILE EXISTS TO PIN. `scripts/content-guard.py`'s own docstring
records it: on 2026-08-29 one `or` rewritten to an `and` left the gate printing
"clean (1 file(s); 0 denylist tokens)" at exit 0, and all 92 tests over that
gate passed. A denylist that is UNAVAILABLE and a denylist that FOUND NOTHING
both exit 0, so an exit code read on its own cannot tell them apart, and a suite
that asserts only exit 0 is asserting nothing at all. Two things separate them
here. The gate's own `build_denylist` reports the two states differently and
that difference is asserted directly. And no clean verdict below is asserted
except against an overlay whose arming was PROVED in the same test file by a
refusal, which is what the floor under the token count is for.

NOT CLAIMED. Nothing about the push-time sibling wall in `scripts/push-all.py`,
nothing about the clean-verdict cache, nothing about the `--all` sweep, nothing
about the inline `content-guard: ok` suppression, and nothing about which real
entities the live overlay does or does not yield.
"""
from __future__ import annotations

import contextlib
import itertools
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.utils.content_denylist import build_denylist

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "scripts" / "content-guard.py"
CONFIG = ROOT / ".pre-commit-config.yaml"

# MEASURED 2026-09-21 against the throwaway overlay built below: 5 tokens in 5
# distinct categories (crm-slug, crm-name, crm-org, crm-email,
# curated:companies). Both floors are asserted OUTSIDE every loop, because the
# loops iterate this same harvest: a harvester silently dropped would take its
# token out of the gate AND out of the loop, and a suite that only looped would
# go green over the smaller list.
#
# 6 and 6 since 2026-09-25 (HOS-130), MEASURED that day: the curated `tokens`
# key gained an entry, because the executive-roster harvest was removed and the
# names it guarded now live under that key. A gate that stopped reading it would
# lose those names in silence, so the key is part of the floor.
TOKEN_FLOOR = 6
CATEGORY_FLOOR = 6

# An invented thread name, hyphenated as the record-name harvest requires.
THREAD_STEM = "zzqx-sablewind-pilot-review"
# An invented Odin principle slug, for the private-names class (F-5.5.20).
PRINCIPLE_STEM = "zzqx-weigh-the-quiet-option-first"

_counter = itertools.count()


@pytest.fixture(scope="module")
def overlay(tmp_path_factory) -> Path:
    """A throwaway DATA overlay of invented entities, in four harvest sources.

    Every value here is made up and carries the `zzqx` sentinel so a grep can
    prove it names nobody.
    """
    root = tmp_path_factory.mktemp("zzqx-overlay")
    # HOS-240 (2026-10-13): the C3 layout the default registry addresses; a
    # card sits under `people/`, where `crm-contacts` resolves.
    contacts = root / "people"
    contacts.mkdir(parents=True)
    (contacts / "zzqxmorwenna-vantril.md").write_text(
        "---\ncompany: Vantril Skyworks\nemail: zzqx@vantril.example\n---\n"
        "An invented contact record.\n", encoding="utf-8")
    # The registry's `threads-business` home, in the `business-threads` group
    # the record-name harvest walks (F-5.3.1).
    threads = root / "company" / "threads"
    threads.mkdir(parents=True)
    (threads / f"{THREAD_STEM}.md").write_text(
        "An invented thread record.\n", encoding="utf-8")
    principles = root / "knowledge" / "odin-brain" / "principles"
    principles.mkdir(parents=True)
    (principles / f"{PRINCIPLE_STEM}.md").write_text(
        "An invented principle record.\n", encoding="utf-8")
    config = root / "config"
    config.mkdir(parents=True)
    (config / "content-denylist.yaml").write_text(
        "companies:\n  - ZzqxGlint\nevents: []\ncodenames: []\n"
        "competitors: []\ntokens:\n  - zzqxtamsin-orrevale\npersonal: []\n", encoding="utf-8")
    return root


@pytest.fixture(scope="module")
def denylist(overlay):
    """The tokens the gate itself harvests from that overlay, asked of the gate.

    Not a copy of what the fixture wrote. `build_denylist` is the function
    `scripts/content-guard.py` calls, so a harvester that stops running takes
    its token out of this list and the floor below goes red.
    """
    dl = build_denylist(overlay)
    if dl.degraded:
        pytest.fail(f"the gate reported its harvest DEGRADED over the throwaway "
                    f"overlay at {overlay}, so the denylist driving every case "
                    f"below is not the one the gate would use. Nothing here "
                    f"tests anything until that is resolved.")
    assert len(dl.tokens) >= TOKEN_FLOOR, (
        f"the gate harvested {len(dl.tokens)} tokens from the throwaway "
        f"overlay, below the {TOKEN_FLOOR} measured 2026-09-21. A harvester has "
        f"stopped running, and the loops below would have gone green over the "
        f"shorter list.")
    categories = set(dl.tokens.values())
    assert len(categories) >= CATEGORY_FLOOR, (
        f"the harvest yielded {len(categories)} token categories, below the "
        f"{CATEGORY_FLOOR} measured 2026-09-21: {sorted(categories)}")
    return dl


@contextlib.contextmanager
def _planted(body: str):
    """One engine-routed text file inside the checkout, gone before the test ends.

    Yields the REPO-RELATIVE path, which is what the pre-commit hook hands the
    gate and the only shape the gate reads. Both halves of that are measured.
    MEASURED 2026-09-21: the same bytes in `tmp_path` exit 0, because
    `engine_text_rels` keeps only candidates that resolve under the workspace
    root; and an ABSOLUTE path to this very file also exits 0, because that
    function strips the leading separator and the resulting `home/...` names
    nothing. Both drops are silent, so a fixture handed over in either shape
    would have left this whole file green over a gate that read no bytes at all.
    Named with the process id so two runs cannot collide, and a `.py`: since
    HOS-232 only a file the engine allowlist covers routes engine.
    """
    target = ROOT / "tests" / f"zzqx-content-probe-{os.getpid()}-{next(_counter)}.py"
    try:
        target.write_text(body, encoding="utf-8")
        yield target.relative_to(ROOT).as_posix()
    finally:
        target.unlink(missing_ok=True)


def _run(data_root: Path, rel: str) -> subprocess.CompletedProcess:
    """The REAL entry point a commit reaches, not an imported helper."""
    return subprocess.run(
        [sys.executable, str(GATE), "--quiet", "--data-root", str(data_root),
         "--files", rel],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300,
    )


def test_every_entity_the_gate_harvests_is_refused_in_an_engine_routed_file(
        overlay, denylist):
    """One subprocess per harvested token. The assertion is the EXIT CODE.

    A message is prose the gate may reword; the exit code is what stops the
    commit.
    """
    survivors: list[str] = []
    for token, category in denylist.tokens.items():
        with _planted(f"A line naming {token} in ordinary prose.\n") as target:
            if _run(overlay, target).returncode == 0:
                survivors.append(f"{token} [{category}]")
    assert not survivors, (
        "the gate passed an engine-routed file carrying an entity its own "
        "denylist holds: " + ", ".join(survivors))


def test_an_engine_routed_file_naming_no_entity_passes_the_same_armed_denylist(
        overlay, denylist):
    """The other direction, and the clean verdict is EARNED rather than assumed.

    The refusal is driven first, in this same test, against the same overlay.
    Without it a 0 here would be indistinguishable from the 2026-08-29 defect,
    where the gate skipped its whole scan and exited 0. With it, an unarmed
    denylist fails on the first assertion and never reaches the second.
    """
    dirty = next(iter(denylist.tokens))
    with _planted(f"A line naming {dirty} in ordinary prose.\n") as target:
        armed = _run(overlay, target)
    assert armed.returncode == 1, (
        "the denylist that is about to certify a file clean did not refuse a "
        f"file carrying one of its own tokens:\n{armed.stdout}\n{armed.stderr}")

    with _planted("Ordinary engine prose, zzqx and nothing else.\n") as target:
        honest = _run(overlay, target)
    assert honest.returncode == 0, (
        "the gate refused an engine-routed file naming no entity at all:\n"
        f"{honest.stdout}\n{honest.stderr}")


def test_an_unavailable_denylist_is_a_different_state_from_one_that_found_nothing(
        overlay, denylist, tmp_path):
    """The defect named in the module docstring, pinned in both halves.

    First in the gate's own data, where the two states are genuinely distinct
    and can be asserted. Then at the exit code, where they are NOT distinct:
    identical bytes, refused under the armed overlay and passed under an absent
    one, both at exit 0 for the second. That is the documented behaviour of this
    gate on a public clone, and it is written down as an assertion rather than
    as prose so that the day it changes, somebody reads this paragraph.
    """
    absent = build_denylist(tmp_path / "no-overlay-here")
    assert absent.degraded is True and not absent.tokens, (
        "a denylist built from a path that is not there must report itself "
        "DEGRADED. If it reports a healthy empty harvest instead, the gate can "
        "no longer tell 'I could not look' from 'I looked and found nothing'.")

    present_but_empty = tmp_path / "empty-overlay"
    present_but_empty.mkdir()
    nothing_found = build_denylist(present_but_empty)
    assert nothing_found.degraded is False and not nothing_found.tokens, (
        "a denylist built from a real but empty overlay must report itself "
        "healthy and empty, which is the state the gate distinguishes from "
        "degraded")

    body = f"A line naming {next(iter(denylist.tokens))} in ordinary prose.\n"
    with _planted(body) as target:
        armed = _run(overlay, target)
        unarmed = _run(tmp_path / "no-overlay-here", target)
    assert armed.returncode == 1, (
        f"the armed overlay failed to refuse:\n{armed.stdout}\n{armed.stderr}")
    assert unarmed.returncode == 0, (
        "MEASURED 2026-09-21: the gate exits 0 when its denylist is "
        "unavailable, over bytes it refuses when armed. A 0 from this gate is "
        "therefore never on its own evidence that a file is clean, which is why "
        "every clean assertion in this file is paired with a refusal proved on "
        "the same overlay. If this now exits non-zero the gate has been "
        f"hardened; update this file and its docstring:\n{unarmed.stdout}\n"
        f"{unarmed.stderr}")


def test_the_gate_is_registered_in_the_config_that_arms_it(denylist):
    """A gate nothing invokes refuses nothing, whatever its code says.

    Registration only. Whether the registration's `files:` pattern selects
    anything is `scripts/check-gate-integrity.py`'s, and it runs on every commit.
    """
    config = CONFIG.read_text(encoding="utf-8")
    assert "content-guard.py" in config, (
        f"content-guard.py is not registered in {CONFIG.name}, so nothing runs "
        f"it at commit time and the cases above prove only that the gate exists "
        f"on disk")


def test_a_tree_that_migrated_and_lost_its_manifest_degrades_the_denylist(
        overlay, denylist, tmp_path):
    """FAILS CLOSED when the registry cannot place the wall's own sources.

    HOS-240, 2026-10-13: the shape changed and the property did not. No
    default name carries a `record:` base any more, so a lost manifest no
    longer reaches the wall; what does is a tree whose contact directory, the
    one `crm-contacts` names, is not there while the tree is. That tree is the
    damaged one below. The paragraph after this one is the 2026-09-25 history.

    Operator decision 2026-09-25. The overlay fixture has no manifest and no
    family root, so the registry answers its pre-move addresses and the
    `denylist` fixture has already proved that harvest healthy: that is the
    honest half. Here the same overlay also holds a record under a family root,
    which is how a tree looks after it migrated and lost `_manifest/`, and the
    registry refuses to answer. MEASURED 2026-09-25 against the previous
    `scripts/utils/content_denylist.py`: that version fell back to the pre-move
    directories, harvested, and reported `degraded` False; the push wall then
    scanned with whatever those stale addresses still held and printed clean.
    """
    import shutil
    damaged = tmp_path / "damaged"
    shutil.copytree(overlay, damaged)
    shutil.rmtree(damaged / "people")  # `config/` stays, so the tree holds data
    assert build_denylist(damaged).degraded is True, (
        "a tree whose contact directory is missing must report the denylist "
        "DEGRADED, so the push wall refuses rather than scanning blind")


def test_a_thread_name_reaches_the_denylist_and_a_lost_thread_home_degrades_it(
        overlay, denylist, tmp_path):
    """F-5.3.1: thread names are read from the registry's thread homes.

    The record-name harvest joined `<data root>/threads`, an address the C3
    move emptied, so no thread name reached the wall and the denylist still
    reported itself healthy. MEASURED 2026-10-16 against the previous
    `scripts/utils/content_denylist.py`: the invented thread below was not
    harvested, and a tree with no thread home at all was not degraded.

    Both halves: the thread stem is harvested and an engine-routed file naming
    it is refused at the real entry point; a tree holding data with no thread
    home on disk is DEGRADED, so the push wall refuses instead of scanning
    without thread names.
    """
    assert denylist.tokens.get(THREAD_STEM) == "record-name", (
        "the invented thread under the registry's thread home was not harvested")
    with _planted(f"A line naming {THREAD_STEM} in ordinary prose.\n") as target:
        refused = _run(overlay, target)
    assert refused.returncode == 1, (
        f"an engine-routed file naming a thread was not refused:\n"
        f"{refused.stdout}\n{refused.stderr}")

    import shutil
    lost = tmp_path / "lost-threads"
    shutil.copytree(overlay, lost)
    shutil.rmtree(lost / "company" / "threads")
    assert build_denylist(lost).degraded is True, (
        "a tree holding data with no thread home must report the denylist "
        "DEGRADED rather than a healthy harvest with no thread name in it")


def test_a_private_record_name_from_the_odin_brain_is_refused(overlay, denylist):
    """F-5.5.20, F-2.25, F-4.36: real record names in engine code, as a class.

    A real principle slug from the Odin brain sat in an engine smoke run, and
    no gate read the brain's file names. MEASURED 2026-10-16: against the
    previous `scripts/utils/content_denylist.py` the invented principle below
    was not harvested; once the brain was read, a scan of the whole engine over
    the live overlay refused eight lines naming seven real records. The harvest is asked of the gate and the refusal of the real entry
    point; the honest half is the clean case in the test above.
    """
    assert denylist.tokens.get(PRINCIPLE_STEM) == "record-name", (
        "the invented principle under the registry's odin-brain was not harvested")
    with _planted(f"A smoke run over {PRINCIPLE_STEM}.\n") as target:
        refused = _run(overlay, target)
    assert refused.returncode == 1, (
        f"an engine-routed file naming an Odin principle was not refused:\n"
        f"{refused.stdout}\n{refused.stderr}")
