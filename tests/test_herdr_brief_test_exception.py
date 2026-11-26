"""GUARANTEE: a brief claiming a test exception is sent only when the operator listed it.

FAILURE B. `scripts/herdr-brief.py` is the guard every HELM brief passes. Rule
M050 admits a test only for failure A or B; the one other path is a test the
operator named, listed in `config/test-exceptions.yaml` and briefed as
`Tests: exception HOS-N, <file>`. If the guard accepted an unlisted pair, a
yard could be sent off to write a test nobody approved, under a line that reads
as if somebody had. This is the guard's bidirectional test (M031.1).

HOW IT IS DRIVEN. `check_tests_line` is called with fixture exceptions files
under `tmp_path`, and one case drives the real entry point `main()` against the
committed file. The assertion is the return code, 0 sent or 7 refused.

THE FAILING HALF AGAINST THE PREVIOUS VERSION. MEASURED 2026-11-23 as a
reproduction run, not here: the herdr-brief.py on `main` before HOS-427 returns
7 for `Tests: exception HOS-414, tests/test_lot_map_station_rules_agree.py`
("not one of the two accepted forms"), so every accept case below is red on it.

NOT CLAIMED. Nothing here checks who added an entry: no gate holds that, and
HELM reviews the file's diff at merge. Nor whether the named test file exists.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "herdr-brief.py"

LISTED = "Tests: exception HOS-414, tests/test_lot_map_station_rules_agree.py"
ENTRY = ("exceptions:\n"
         "  - issue: HOS-414\n"
         "    file: tests/test_lot_map_station_rules_agree.py\n"
         "    decided: 2026-11-23\n"
         "    by: operator\n"
         "    why: fixture\n")


def _load():
    spec = importlib.util.spec_from_file_location("herdr_brief_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HB = _load()


@pytest.fixture
def listed(tmp_path: Path) -> Path:
    path = tmp_path / "test-exceptions.yaml"
    path.write_text(ENTRY, encoding="utf-8")
    return path


@pytest.mark.parametrize("issues", [None, ["HOS-414"]])
def test_a_listed_pair_is_accepted(listed: Path, issues) -> None:
    assert HB.check_tests_line(LISTED, issues, listed) == 0


@pytest.mark.parametrize("text, issues", [
    (LISTED, ["HOS-1"]),
    (LISTED, []),
    ("Tests: exception HOS-414, tests/other.py", None),
    ("Tests: exception HOS-9, tests/test_lot_map_station_rules_agree.py", None),
])
def test_an_unlisted_or_unallocated_exception_is_refused(listed: Path, text, issues) -> None:
    assert HB.check_tests_line(text, issues, listed) == 7


def test_a_missing_or_misshapen_file_refuses_every_exception(tmp_path: Path) -> None:
    assert HB.check_tests_line(LISTED, None, tmp_path / "absent.yaml") == 7
    wrong = tmp_path / "wrong.yaml"
    wrong.write_text("other:\n  - issue: HOS-414\n", encoding="utf-8")
    assert HB.check_tests_line(LISTED, None, wrong) == 7


def test_a_file_that_fails_while_parsing_refuses_every_exception(tmp_path: Path) -> None:
    """MEASURED 2026-11-23 on 2c852376: `decided: 2026-13-23` raised ValueError out of the check."""
    bad = tmp_path / "bad-date.yaml"
    bad.write_text(ENTRY.replace("2026-11-23", "2026-13-23"), encoding="utf-8")
    assert HB.check_tests_line(LISTED, None, bad) == 7


@pytest.mark.parametrize("text", ["Tests: none (Fix-Note)", "Tests: failure A, x"])
def test_the_two_existing_forms_still_pass(tmp_path: Path, text) -> None:
    assert HB.check_tests_line(text, None, tmp_path / "absent.yaml") == 0


def test_the_entry_point_refuses_an_exception_for_another_issue(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["herdr-brief.py", "t0", LISTED, "--issue", "HOS-1"])
    assert HB.main() == 7
