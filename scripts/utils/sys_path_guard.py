"""`sys.path` is a shared index, and a test that leaves an entry on it fails
whichever test reads the path next.

`sys.path` is process-global and an xdist worker runs hundreds of tests in one
process, so an entry a test body adds and never removes stands for every test
that lands later on that worker. The victim is whichever test reads the path
next, and it is the victim that goes red, in a run that cannot be repeated
because the pairing depends on how `--dist load` dealt the items that time.

MEASURED 2026-09-13: `tests/test_impeccable_engine.py::test_cap1_default_run_
does_not_invoke_the_deep_engine`, whose first assertion is
`str(ROOT / "scripts") not in sys.path`, failed in the 21:00 pre-push gate
(26,849 passed, 12 failed, xdist on a loaded machine) and passed in the 20:34
full run and in 30 of 30 isolated runs. No traceback survived; the gate keeps
the summary. Nothing in that test reads a clock, sets a deadline or waits on a
child, so the only thing that could differ between the two runs is which tests
shared its worker before it.

The IMPORT-TIME shape was guarded separately, by collecting the suite in a
child and diffing `sys.path` before and after; that went in the 2026-09-21
test-suite split. It could not see a mutation made inside a test BODY anyway,
because collection never runs one. The pair of hooks here is the other half,
and it is the half that still runs: snapshot
before the item's setup, compare after its teardown, and report at the item
that changed it.

WHY A PLUGIN MODULE AND NOT A PAIR OF FUNCTIONS IN `tests/conftest.py`. It was
a pair of functions there, and so was the pinned-environment guard that landed
the same evening from another worktree. Both defined `pytest_runtest_setup` and
`pytest_runtest_teardown`; they sat in different regions of the file, so git
merged them without a conflict, and Python kept the LAST definition of each
name. MEASURED 2026-09-13 on `main` at e19900ee:
`grep -c "^def pytest_runtest_setup" tests/conftest.py` printed 2, the
environment pair won, and `tests/test_a_path_entry_a_test_body_left_behind.py`
reported "1 failed, 1 passed" in its child, the exact pre-guard shape: the
witness paid and the leaker walked. Each yard's own full run was green, because
each yard had one pair. A hook implementation that lives in its own module is
registered by pluggy beside every other module's, so two guards for two kinds
of leak never compete for a name; `scripts/utils/logging_tree_guard.py` is the
precedent. A test refused the next same-module collision at the AST until the
2026-09-21 split, so read the merged conftest yourself after any merge that
touches it.

Two hooks rather than an autouse fixture, and the reason is measured.
`monkeypatch` is torn down AFTER an autouse fixture that does not request it
(`--setup-show`, 2026-09-13: `monkeypatch`, then the data-root pin that
requests it, then the guard fixture, torn down in reverse), so a
`syspath_prepend` was still in force when the fixture compared, and the one
shape the message recommends was reported as a leak. The teardown hook's
post-yield runs after every function-scoped fixture of the item is gone. Not
`pytest_runtest_protocol`: the egress gate in `tests/conftest.py` already
wraps it, and a second function of that name in one module silently replaces
the first, which is the very shape recorded above.

The window also encloses the setup of a higher-scoped fixture the item is
first to need and the teardown of one it is last to need, so a module-scoped
fixture that inserts on setup and restores on teardown would be reported
twice, as an addition on the first item and a removal on the last. No fixture
in the tree does that today; one that must should restore inside the item that
inserted, or use `monkeypatch` at its own scope.

Two design points. It RESTORES as well as fails, so one leak is one error at
its source rather than an error there plus an unrepeatable failure on a
stranger. And what it REPORTS is membership, an entry that was not resolvable
before and is after, or one that was and is not, while what it restores is the
whole list. MEASURED 2026-09-13 with a whole-list comparison: the FIRST item of
every session went red, honest or not, because `_pin_model_resolution` in
`tests/conftest.py` imports `scripts.utils.claude_models` lazily and its line
45 inserts a second copy of the repo root at import; and so did every test that
loads a script by path, because `scripts/visual-discipline-check.py` and its
kind do the same. A second copy of an entry that is already present resolves
nothing new. The restore is still whole-list, since a duplicate is what
`list.remove` leaves behind.

Mutation-verified 2026-09-13 (7 of 8 planted breaks caught). Armed by
`tests/conftest.py` through `pytest_plugins`; its own test left in the
2026-09-21 test-suite split.
"""

from __future__ import annotations

import sys

import pytest

_SYS_PATH_BEFORE_ITEM = pytest.StashKey[list]()


def membership_drift(before: list[str], after: list[str]) -> tuple[list[str], list[str]]:
    """The entries resolvable now and not before, and the reverse.

    Membership, not position or multiplicity: a second copy of an entry that
    is already present resolves nothing new, and every script loaded by path
    inserts one.
    """
    added = [p for p in after if p not in before]
    removed = [p for p in before if p not in after]
    return added, removed


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_setup(item):
    """Snapshot `sys.path` before any fixture of the item is set up."""
    item.stash[_SYS_PATH_BEFORE_ITEM] = sys.path[:]
    return (yield)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    """Compare after every fixture of the item is torn down; fail the leaker.

    `tryfirst` on a wrapper makes it the OUTERMOST, so this post-yield runs
    after every other wrapper's. Raising here is the documented way for a
    new-style wrapper to replace the hook's result: pytest records it as the
    item's own teardown error, so the summary line names the leaker.
    """
    result = yield
    before = item.stash.get(_SYS_PATH_BEFORE_ITEM, None)
    if before is None:
        return result
    after = sys.path[:]
    if after == before:
        return result
    sys.path[:] = before
    added, removed = membership_drift(before, after)
    if not added and not removed:
        return result
    pytest.fail(
        f"{item.nodeid} left sys.path changed (added={added}, "
        f"removed={removed}). It has been restored so no later test on this "
        "worker inherits it, but the leak is this test's. Snapshot "
        "`sys.path[:]` before the insert and assign it back in a `finally`, "
        "or use `monkeypatch.syspath_prepend`; do not `remove()` one string, "
        "because the module you load may insert the same directory itself "
        "and two copies go on.",
        pytrace=False,
    )
