"""The logging tree is a shared index, and a test that leaves it configured
fails the next test that reads it.

There is ONE `logging` tree per process. The root logger's level and handlers,
every named logger's level, its `propagate` flag and its handler list, and the
process-wide `logging.disable` floor are all global state, and pytest restores
none of it between tests. It restores only what it changed itself: `caplog`
puts a logger's level back when `at_level` exits, and nothing else.

So a test that calls `logging.basicConfig(level=logging.INFO)`, or a module
that calls it at import time, or a test that clears the root handlers and
never puts them back, changes the answer of every later test in the same
worker that asks "was anything logged?". The failure lands on the LATER test,
which is innocent and passes alone, and the run reports it under a name that
has nothing to do with the cause.

MEASURED 2026-09-13 in the full pre-push run, worker gw8:
`tests/test_a_rules_engine_that_crashed_on_a_config_it_could_parse.py::
test_a_well_formed_config_is_read_exactly_as_before` asserted that
`caplog.records` was empty under `caplog.at_level(logging.WARNING, logger=...)`
and found one INFO record, "email-triage-rules.yaml reloaded from ...", which
had also been printed to the test's captured stderr by a `StreamHandler` nobody
in that file had installed. Alone, the file passed. The record was emitted by
`RulesEngine.__init__` BEFORE the `at_level` block opened, so the named
logger's level was still NOTSET and the record's fate was decided by the ROOT.

The root had been rewritten at COLLECTION, before any test ran, by a
third-party package that reconfigured logging on import (MEASURED 2026-09-13;
the package left on 2026-10-04, HOS-198), which is why a per-test check alone
would never have named it.

This module is the guard, in two windows:

* per TEST: a snapshot of the tree at the outermost `pytest_runtest_setup`, a
  diff at the outermost `pytest_runtest_teardown`, an error at teardown that
  names the test which made the change and lists what changed, and a RESTORE
  of the snapshot so one leak produces one error rather than a cascade under
  later names;
* per COLLECTED MODULE: the same bracket around `pytest_make_collect_report`
  for a `Module`, whose import is where module-level code runs. A change turns
  the module's collect report into a failure naming the file, and the tree is
  restored so the modules after it collect against a clean root.

Armed by `tests/conftest.py` through `pytest_plugins`; the pure functions are
importable without pytest so a probe can take the same snapshot by hand.

What counts as a change:

* root: level, the handler list (by identity, and each handler's own level),
  and the `disabled` flag;
* every named logger that existed before the window: level, `propagate`,
  `disabled`, handler list (identity and level);
* every named logger CREATED inside the window: it may exist, because
  importing a module creates one, and it may carry a level or `propagate`
  flag, because a library configures its OWN subtree at import (`olefile`
  sets its logger to CRITICAL+1) and nothing outside that subtree can see
  either; but it may not carry a handler, which writes somewhere;
* the process-wide `logging.disable` floor.

What does not count:

* pytest's own per-phase capture handlers, added and removed INSIDE the
  per-test window, so the outermost snapshots never see them;
* a `NullHandler`, anywhere: it emits nothing, and a test that replaces one
  with another has changed nothing a later test can observe;
* a change made by importing a package in `IMPORT_TIME_CONFIGURATORS`. The
  tree is put back silently. The library's misbehaviour is not the test's; an
  AST sweep held the repository's own scripts to the pattern until the
  2026-09-21 test-suite split, and nothing sweeps for it now.

This guard is still armed. Its own bidirectional test, which drove a child
pytest through `pytester` in both directions, went in the 2026-09-21 test-suite
split, so nothing now proves that it still refuses a leaked tree.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field

# A logger's defaults, which a logger created during a test is held to.
_DEFAULT_LEVEL = logging.NOTSET

# Packages whose IMPORT configures a logger that is not theirs to configure. A
# change that coincides with one of these first appearing in `sys.modules` is
# put back without an error. Each entry carries its measurement, taken
# 2026-09-13 in a fresh `.venv/bin/python`, and the limit of the allowance is
# stated once: a window in which a listed package is first imported is put back
# WITHOUT being examined, so a leak of the test's own in that same window is
# not reported. Add a name only with a measurement beside it.
#
#   langfuse  `httpx`: `NOTSET []` -> `WARNING [StreamHandler]`
#             (langfuse/logger.py); an EXISTING logger, so a change and not a
#             creation. It was reached at collection by an `importorskip` in a
#             test file the 2026-09-21 split removed, so nothing in the live
#             suite imports it today. The allowance is kept for the day one
#             does; the measurement stands as taken.
#   telethon  `pyasn1`: absent -> `DEBUG [StreamHandler]` (pyasn1/debug.py, a
#             handler at DEBUG to stderr). Reached the same way, by an
#             `importorskip` that the same split removed.
IMPORT_TIME_CONFIGURATORS: frozenset[str] = frozenset({"langfuse", "telethon"})


def _counts(handler: logging.Handler) -> bool:
    return type(handler) is not logging.NullHandler


@dataclass(frozen=True)
class LoggerState:
    """What one logger looks like: everything a caller can set on it that
    changes whether, and where, a record emitted later is delivered."""

    level: int
    propagate: bool
    disabled: bool
    # (handler object, handler level) pairs. The object is kept, not its id,
    # so `restore` can put it back.
    handlers: tuple[tuple[logging.Handler, int], ...]

    def handler_ids(self) -> tuple[tuple[int, int], ...]:
        return tuple((id(h), lvl) for h, lvl in self.handlers if _counts(h))

    def writes_somewhere(self) -> bool:
        """True when a record reaching this logger can leave the process
        through a handler of its own. The one thing a logger that did not
        exist before the window may not have acquired."""
        return any(_counts(h) for h, _ in self.handlers)

    def describe(self) -> str:
        handlers = ", ".join(
            f"{type(h).__name__}@{logging.getLevelName(lvl)}" for h, lvl in self.handlers
        ) or "no handlers"
        flags = "" if self.propagate else ", propagate=False"
        flags += ", disabled" if self.disabled else ""
        return f"level={logging.getLevelName(self.level)}, [{handlers}]{flags}"


@dataclass(frozen=True)
class TreeSnapshot:
    root: LoggerState
    loggers: dict[str, LoggerState] = field(default_factory=dict)
    disable_floor: int = logging.NOTSET
    # Which of IMPORT_TIME_CONFIGURATORS were already imported.
    configurators_loaded: frozenset[str] = frozenset()


def _state_of(logger: logging.Logger) -> LoggerState:
    return LoggerState(
        level=logger.level,
        propagate=logger.propagate,
        disabled=logger.disabled,
        handlers=tuple((h, h.level) for h in logger.handlers),
    )


def snapshot() -> TreeSnapshot:
    """Read the whole tree. Read-only: creates no logger."""
    manager = logging.Logger.manager
    loggers = {
        name: _state_of(obj)
        for name, obj in list(manager.loggerDict.items())
        if isinstance(obj, logging.Logger)
    }
    return TreeSnapshot(
        root=_state_of(logging.getLogger()),
        loggers=loggers,
        disable_floor=manager.disable,
        configurators_loaded=frozenset(
            name for name in IMPORT_TIME_CONFIGURATORS if name in sys.modules
        ),
    )


def _differs(before: LoggerState, after: LoggerState) -> bool:
    return (
        before.level != after.level
        or before.propagate != after.propagate
        or before.disabled != after.disabled
        or before.handler_ids() != after.handler_ids()
    )


def diff(before: TreeSnapshot, after: TreeSnapshot) -> list[str]:
    """Every way `after` differs from `before`, one line each, in a stable
    order: root first, then the disable floor, then named loggers by name."""
    lines: list[str] = []
    if _differs(before.root, after.root):
        lines.append(f"root logger: {before.root.describe()} -> {after.root.describe()}")
    if before.disable_floor != after.disable_floor:
        lines.append(
            "logging.disable floor: "
            f"{logging.getLevelName(before.disable_floor)} -> "
            f"{logging.getLevelName(after.disable_floor)}"
        )
    for name in sorted(set(before.loggers) | set(after.loggers)):
        was = before.loggers.get(name)
        now = after.loggers.get(name)
        if was is None:
            # Created inside the window. Existing is fine; writing is not.
            if now is not None and now.writes_somewhere():
                lines.append(f"logger {name!r} created and left at {now.describe()}")
        elif now is None:
            # A logger cannot be deleted through the public API; a test that
            # reached into `loggerDict` did so, and the next `getLogger` for
            # that name returns a fresh object nobody's earlier handle reaches.
            lines.append(f"logger {name!r} removed from the tree")
        elif _differs(was, now):
            lines.append(f"logger {name!r}: {was.describe()} -> {now.describe()}")
    return lines


def excused_by_an_import(before: TreeSnapshot, after: TreeSnapshot) -> frozenset[str]:
    """The configurator packages that were imported inside the window."""
    return after.configurators_loaded - before.configurators_loaded


def _restore_logger(logger: logging.Logger, state: LoggerState) -> None:
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    for handler, level in state.handlers:
        handler.setLevel(level)
        logger.addHandler(handler)
    logger.setLevel(state.level)
    logger.propagate = state.propagate
    logger.disabled = state.disabled


def restore(before: TreeSnapshot) -> None:
    """Put the tree back the way `before` saw it, so one leaking test costs
    one error rather than an error under every later name in the worker.

    A logger created inside the window is reset rather than deleted:
    deleting it would orphan the handle the module that created it holds.
    """
    _restore_logger(logging.getLogger(), before.root)
    logging.Logger.manager.disable = before.disable_floor
    manager = logging.Logger.manager
    for name, obj in list(manager.loggerDict.items()):
        if not isinstance(obj, logging.Logger):
            continue
        state = before.loggers.get(name)
        if state is None:
            reset_logger(obj)
        else:
            _restore_logger(obj, state)


def reset_logger(logger: logging.Logger) -> None:
    """Back to the state `getLogger` hands out: NOTSET, propagating, enabled,
    and nothing attached but the `NullHandler`s a library may have installed.
    The one reset for a logger a test built for itself; `scratch_logger`
    below applies it at teardown, and `restore` applies it to a logger that
    did not exist before the window."""
    _restore_logger(logger, LoggerState(_DEFAULT_LEVEL, True, False, tuple(
        (h, h.level) for h in logger.handlers if not _counts(h)
    )))


def settle(before: TreeSnapshot) -> list[str]:
    """Compare the tree with `before`, put it back, and return what changed;
    an empty list when nothing did, or when an import in
    `IMPORT_TIME_CONFIGURATORS` explains it."""
    after = snapshot()
    changes = diff(before, after)
    if not changes:
        return []
    restore(before)
    if excused_by_an_import(before, after):
        return []
    return changes


class LoggingTreeChanged(AssertionError):
    """Raised at teardown of the test that left the logging tree changed."""


_ADVICE = (
    "Restore what you configure (`caplog.at_level`, a monkeypatch, a "
    "finally, or `snapshot()`/`restore()` from this module), or move a "
    "script's import-time logging.basicConfig under its main(). The tree has "
    "been put back for what follows. What changed:\n  "
)


# ---------------------------------------------------------------------------
# The pytest half. Import-guarded so `snapshot`/`diff` stay usable in a plain
# interpreter, and registered through `pytest_plugins` in tests/conftest.py.
# ---------------------------------------------------------------------------
try:  # pragma: no cover - exercised through the child pytest in the test file
    import pytest
except ImportError:  # pragma: no cover
    pytest = None  # type: ignore[assignment]

if pytest is not None:
    _SNAPSHOT_KEY: "pytest.StashKey[TreeSnapshot]" = pytest.StashKey()

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_setup(item):
        # `tryfirst` on a wrapper makes it the OUTERMOST one, so the snapshot
        # is taken before pytest's logging plugin attaches its setup-phase
        # capture handlers to the root.
        item.stash[_SNAPSHOT_KEY] = snapshot()
        return (yield)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_teardown(item, nextitem):
        # Outermost again: by the time control returns here every fixture has
        # torn down, `caplog` has put back the levels it set, and the logging
        # plugin has detached its teardown-phase handlers. What is left is what
        # the NEXT test will inherit.
        result = yield
        before = item.stash.get(_SNAPSHOT_KEY, None)
        if before is None:
            return result
        changes = settle(before)
        if changes:
            raise LoggingTreeChanged(
                f"{item.nodeid} left the process-wide logging tree changed, and "
                "the next test in this worker would inherit it. " + _ADVICE
                + "\n  ".join(changes)
            )
        return result

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_make_collect_report(collector):
        # A Module's collect() is where its import runs, and every worker
        # imports every module before any test starts, so a change here is
        # inherited by the whole run. The report is turned into a collection
        # failure under the module's own path.
        if not isinstance(collector, pytest.Module):
            return (yield)
        before = snapshot()
        report = yield
        changes = settle(before)
        if changes:
            report.outcome = "failed"
            report.longrepr = (
                f"importing {collector.nodeid} changed the process-wide logging "
                "tree, and every test in this worker would inherit it. " + _ADVICE
                + "\n  ".join(changes)
            )
        return report
