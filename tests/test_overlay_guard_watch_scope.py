"""The overlay write guard's snapshot skips two bulky runtime subtrees, and only those.

Failure B: a run that writes into the operator's live overlay must be reported.
Narrowing what the snapshot walks is the one change that can silence that report,
so this pins both halves on a fake overlay driven through the real snapshot:

* a write under one of the two `_UNWATCHED_IN_STORE` subtrees is not reported,
  and the walk never descends into them (pruned, not filtered afterwards);
* a write anywhere else in the runtime store, and in a record directory, still is.

The store's directory name is asked of the module, never spelled here, because
the guard derives it from the overlay's registry.
"""
import os

import pytest

from scripts.utils import overlay_write_guard as g

_STORE = "store-under-test"
# Spelled here rather than read from the module, so the first test fails on
# BEHAVIOUR against a guard that still walks them, not on a missing name.
_TWO = ("checkout-scratch", "hindsight-snapshot")


@pytest.fixture
def fake_overlay(tmp_path, monkeypatch):
    root = tmp_path / "overlay"
    for name in _TWO:
        (root / _STORE / name / "deep").mkdir(parents=True)
        (root / _STORE / name / "deep" / "old.md").write_text("old")
    (root / _STORE / "hook-state").mkdir(parents=True)
    (root / _STORE / "hook-state" / "s.json").write_text("{}")
    (root / "status" / "threads").mkdir(parents=True)
    (root / "status" / "threads" / "t.md").write_text("thread")
    monkeypatch.setattr(g, "_watched_roots", lambda: {"fake overlay": root})
    monkeypatch.setattr(g, "_record_dir_rel", lambda: f"{_STORE}/overlay-guard/logs")
    return root


def test_the_two_runtime_subtrees_are_pruned_and_not_reported(fake_overlay, monkeypatch):
    real_walk = os.walk
    visited = []

    def recording_walk(*args, **kwargs):
        for entry in real_walk(*args, **kwargs):
            visited.append(entry[0])
            yield entry

    monkeypatch.setattr(g.os, "walk", recording_walk)
    before = g._watch_snapshot()
    for name in _TWO:
        (fake_overlay / _STORE / name / "deep" / "new.md").write_text("new")
        (fake_overlay / _STORE / name / "deep" / "old.md").write_text("rewritten")

    assert g.watch_complaints(before, g._watch_snapshot()) == []
    for name in _TWO:
        pruned = str(fake_overlay / _STORE / name)
        assert not any(v == pruned or v.startswith(pruned + os.sep) for v in visited), name
    # Exactly these two, each with a reason a reader can act on.
    assert set(g._UNWATCHED_IN_STORE) == set(_TWO)
    assert all(len(reason) > 40 for reason in g._UNWATCHED_IN_STORE.values())


@pytest.mark.parametrize("rel", [
    (_STORE, "hook-state", "new.json"),
    (_STORE, "new-kind", "cache", "x.bin"),
    ("status", "threads", "new.md"),
    # A nested directory sharing one of the two names, outside the store, is watched.
    ("status", "checkout-scratch", "x.md"),
])
def test_a_write_elsewhere_is_still_reported(fake_overlay, rel):
    before = g._watch_snapshot()
    target = fake_overlay.joinpath(*rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("new")

    complaints = g.watch_complaints(before, g._watch_snapshot())
    assert len(complaints) == 1 and "/".join(rel) in complaints[0], complaints
