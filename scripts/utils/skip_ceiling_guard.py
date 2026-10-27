"""A skip count only the night could see, made visible to the ordinary run.

The defect
----------
`config/nightly-skip-baseline.json` has carried a `max_skips` ceiling since
2026-09-05, and exactly one thing read it: `scripts/nightly-refresh.py`, once a night, under
a systemd timer. MEASURED 2026-09-18 in HELM and reported to this worktree: the
full suite there was `28023 passed, 0 failed, 74 skipped`, fourteen times the
ceiling committed then (5), while the nightly unit sat in `failed` and had moved no green
marker for two weeks. Twelve of those skips were later shown to be tests knocking
at addresses that no longer exist, hiding two live breakages, and one bench
skipped 48 of its 49 tests and always had. Nothing an operator or a push could
run said any of it, because the only reader of the ceiling was a process that was
not running.

A pass over a suite that did not run is not a pass. This plugin puts that
sentence in the same process as the tests, so `scripts/run-tests.py`, the CI job
and a bare `pytest` all answer it.

What it does
------------
On a FULL run it compares pytest's own skipped count against the committed
ceiling and, over it, fails the session. On a narrow run it does nothing.

The count is pytest's, not a reading of pytest's prose. `nightly-refresh.py` has
to parse the summary line because it drives pytest as a subprocess; this runs
INSIDE the session, so it asks the same object the summary line is rendered
from. CONTRACT READ FROM SOURCE 2026-09-18 against pytest 9.1.1 installed in
`.venv`: `_pytest.terminal.TerminalReporter._build_normal_summary_stats_line`
builds each `"N skipped"` part as `len(self._get_reports_to_display(key))`, and
`_get_reports_to_display` is
`[x for x in self.stats.get(key, []) if getattr(x, "count_towards_summary", True)]`.
`_count_skipped` below is that expression and nothing else. Re-read those two
functions when the pin in `pyproject.toml` moves; a count that drifts from the
printed one would be a gate arguing with the summary above it.

Why a plugin module and not a hook in `tests/conftest.py`
--------------------------------------------------------
Two worktrees put a `pytest_runtest_setup` pair each into that conftest on
2026-09-13; git merged them with no conflict and Python kept only the second, so
one guard was dead on `main` while every yard was green. A guard that adds a
`pytest_*` hook goes in its own module and is named in `pytest_plugins`, where
pluggy calls every module's implementation. A test held that until the
2026-09-21 test-suite split; the rule stands, and reading the merged conftest
is now the only thing that enforces it.

When it arms, and what that costs
---------------------------------
The ceiling was measured for the WHOLE suite, so it is applied only to a run of
the whole suite: `_narrow_reason` returns None only when the positional arguments
are the configured `testpaths` themselves, there is no `-k`, no `--deselect`, no
`--last-failed`/`--stepwise` narrowing, no `--collect-only`, and the `-m`
expression is one of the three the repository's own entry points use (see
`_GATE_MARKEXPRS`, which `scripts/run-tests.py:build_command` is the source of;
a test held the two against drift until the 2026-09-21 test-suite split).

The price, stated rather than implied. **A narrow run is not covered at all**:
`pytest tests/security` may skip every test it collects and this says nothing, so
the guarantee bought here is "the full run cannot be hollow", never "no skip goes
unnoticed". And **a full run in an environment that honestly cannot reach the
tools the suite gates on will go red**: MEASURED 2026-09-05 and recorded in the
baseline file, the same suite skipped 5 in HELM and 52 from a worktree. That is
the intended answer for a worktree pretending to be the night, and it is the
reason the number in the baseline file is not something to tune when this goes
red. A jump in skips means tests stopped running; find what the launcher can no
longer reach before touching the ceiling.

Tests: none live. Both directions, including under `-n auto`, were covered until
the 2026-09-21 test-suite split. This plugin is still registered in
`tests/conftest.py` and still arms; only its proof is gone.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts.utils.skip_ceiling import SKIP_BASELINE_REL, read_skip_ceiling

#: The mark expressions a full-suite entry point uses. `scripts/run-tests.py`
#: `build_command` is the source: it passes `-m "not acceptance"` for the
#: regression gate and `-m acceptance` for the sign-off gates, and a bare pytest
#: passes none. Any OTHER expression is a selection the ceiling was never
#: measured for, so the gate steps aside rather than judging it.
#: This set was derived from that function and compared, so a drift went red.
#: Since the 2026-09-21 test-suite split nothing derives it, so changing
#: `build_command` means changing this frozenset in the same edit.
_GATE_MARKEXPRS = frozenset({"", "not acceptance", "acceptance"})

#: Stash key on `config` for the computed verdict, so `pytest_terminal_summary`
#: and `pytest_sessionfinish` cannot disagree about what was decided.
_VERDICT_ATTR = "_skip_ceiling_verdict"


class Verdict:
    """What the guard decided, and the words for it."""

    def __init__(self, armed: bool, ok: bool, message: str = "",
                 skipped: int = 0, ceiling: int | None = None,
                 reason: str = "") -> None:
        self.armed = armed
        self.ok = ok
        self.message = message
        self.skipped = skipped
        self.ceiling = ceiling
        self.reason = reason


# ============================================================
# The rule, pure, so it can be exercised without a session
# ============================================================

def _count_skipped(stats: dict[str, list[Any]]) -> int:
    """The number pytest prints as `N skipped`. See the module docstring."""
    return len([r for r in stats.get("skipped", [])
                if getattr(r, "count_towards_summary", True)])


def _testpath_dirs(config: Any) -> list[Path]:
    """The directories `testpaths` names, resolved against the rootdir."""
    try:
        configured = list(config.getini("testpaths"))
    except (ValueError, KeyError):
        return []
    return [(Path(config.rootpath) / entry).resolve() for entry in configured]


def _narrow_reason(config: Any) -> str | None:
    """None when this is a full run; otherwise the words for why it is not.

    Every branch here answers the same question: was the ceiling in the baseline
    file measured over the set of tests this run selected? Only a run over the
    whole `testpaths` with no keyword, node or history narrowing can say yes.
    """
    option = config.option

    if getattr(option, "collectonly", False):
        return "--collect-only ran no tests"
    if getattr(option, "keyword", ""):
        return "-k narrows the run"
    if getattr(option, "deselect", None):
        return "--deselect narrows the run"
    if getattr(option, "lf", False):
        return "--last-failed narrows the run"
    if getattr(option, "stepwise", False):
        return "--stepwise narrows the run"
    # `--failed-first` is deliberately absent: it REORDERS the full selection
    # and drops nothing, so a run carrying it is still a full run.

    markexpr = " ".join(str(getattr(option, "markexpr", "") or "").split())
    if markexpr not in _GATE_MARKEXPRS:
        return f"-m {markexpr!r} is not a full-suite selection"

    roots = _testpath_dirs(config)
    if not roots:
        return "no testpaths are configured, so a full run cannot be recognised"

    if not config.args:
        return "nothing was selected"

    invocation_dir = Path(config.invocation_params.dir)
    for arg in config.args:
        target = str(arg).split("::", 1)[0]
        if not target:
            return "an argument selects nodes rather than a path"
        resolved = (invocation_dir / target).resolve()
        if resolved not in roots:
            return f"{target} is narrower than the suite root"
    return None


def verdict_for(skipped: int, ceiling: int | None, narrow: str | None,
                baseline: Path) -> Verdict:
    """Decide, given the three inputs. Pure: the tests drive this directly."""
    if narrow is not None:
        return Verdict(armed=False, ok=True, reason=narrow, skipped=skipped)
    if ceiling is None:
        return Verdict(
            armed=True, ok=False, skipped=skipped, ceiling=None,
            message=(
                "the skip ceiling could not be read, so this run certifies "
                f"nothing.\nExpected a JSON object with a non-negative integer "
                f'"max_skips" at {baseline}.\nThat file is what tells a hollow '
                "pass from a real one."))
    if skipped > ceiling:
        return Verdict(
            armed=True, ok=False, skipped=skipped, ceiling=ceiling,
            message=(
                f"the suite SKIPPED {skipped} tests against a committed ceiling "
                f"of {ceiling}.\nA pass over a suite that did not run is not a "
                f"pass. Ceiling: {baseline}\nA jump means tests stopped running: "
                "find what this launcher can no longer reach (gh, git-lfs, node, "
                "npx, marp, uv, pre-commit, claude, herdr, a session bus, the "
                "codegraph index) before touching that file.\nList them with: "
                "python scripts/run-tests.py  # then read the -rs short summary"))
    return Verdict(armed=True, ok=True, skipped=skipped, ceiling=ceiling)


# ============================================================
# The pytest hooks
# ============================================================

def _compute(config: Any) -> Verdict:
    """Decide once per session and remember it on the config object."""
    cached = getattr(config, _VERDICT_ATTR, None)
    if cached is not None:
        return cached

    narrow = _narrow_reason(config)
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        # Nothing in this process counted the reports, so nothing here can say
        # what an honest run looked like. Refuse on a full run for the same
        # reason an unreadable ceiling refuses; step aside on a narrow one,
        # where the guard was never going to speak anyway.
        result = Verdict(
            armed=narrow is None, ok=narrow is not None, reason=narrow or "",
            message=("the terminal reporter is not loaded, so the skipped count "
                     "cannot be read and this run certifies nothing"))
    else:
        root = Path(config.rootpath)
        result = verdict_for(
            skipped=_count_skipped(reporter.stats),
            ceiling=read_skip_ceiling(root),
            narrow=narrow,
            baseline=root / SKIP_BASELINE_REL)
    setattr(config, _VERDICT_ATTR, result)
    return result


def _is_worker(config: Any) -> bool:
    """True inside an xdist worker.

    Workers see only their own shard, and the controller receives every worker's
    reports through `pytest_runtest_logreport`, so the controller's stats are the
    whole run and a worker's are a fraction of it. Judging in a worker would
    compare a shard against a whole-suite ceiling.
    """
    return hasattr(config, "workerinput")


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    if _is_worker(config):
        return
    result = _compute(config)
    if result.armed and not result.ok:
        terminalreporter.write_sep("=", "skip ceiling", red=True, bold=True)
        for line in result.message.splitlines():
            terminalreporter.write_line(line, red=True)


def pytest_sessionfinish(session, exitstatus) -> None:
    config = session.config
    if _is_worker(config):
        return
    result = _compute(config)
    if result.armed and not result.ok and session.exitstatus == 0:
        # 1 is pytest's own TESTS_FAILED. A run that skipped its way past the
        # ceiling failed to establish what it claims to establish, and the exit
        # code is the only part of that a push hook or CI job reads.
        session.exitstatus = 1
