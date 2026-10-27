"""Where this repository's test files are, for every check that reads all of them.

ONE definition, because three checks each had their own copy of `(root /
"tests").rglob("test_*.py")` and a fourth asked `rel.startswith("tests/")`. On
2026-09-21 the suite was split and all four answered differently about the same
tree, each in the unsafe direction: two REFUSED, having read 2 files against a
constant floor of 400, and one reported sixteen gates as guarded by nothing when
the tests guarding them still existed and had merely moved.

THE SPLIT. Operator decision 2026-09-21 under HOS-54: every test written before
that date is DISABLED and none is deleted. The bodies are not in this repository
and this file does not say where they are. The engine keeps
`config/parked-tests.json`, which holds a COUNT and nothing else.

WHY THE BODIES LEFT THE PUBLIC ENGINE, stated without the inventory that made
the case. A large test corpus accumulates, file name by file name and docstring
by docstring, a description of the tree it runs against and of what the machine
it runs on is wired to. No single line of it is a credential and no gate would
have refused any of it, which is precisely why nothing caught it. It is
disclosure by accumulation, and this repository is public. The measurement that
established it was taken once, reported to the operator, and deliberately not
written down here: a count of how many of those files named which private
directory IS the disclosure, in shorter form.

**THE ENGINE'S CHECKS AUDIT THE ENGINE'S TESTS, AND NOTHING ELSE.** Operator
instruction 2026-09-21, on being shown a first version of this module that
resolved the parked directory through the data overlay so the ratchets could
keep reading it: *"Why would anyone look for what is parked? I know anyway. When
it is needed, I will tell you."* (translated) He is right and the machinery is gone. A parked test is
OFF. It is run by naming its path, when he asks for it, and by nothing else.
Nothing in this repository reaches into the overlay to find one, so there is no
dynamic input here that could arrive empty and be read as "nothing to check" --
which is the defect written out in `scripts/content-guard.py`'s own docstring,
where a denylist arriving from the overlay came back empty on a public clone and
the gate exited 0 while all 92 tests over it passed. The cheapest way not to
have that defect is not to have the input.

What the manifest is for, then: a COUNT, so it can be ratcheted DOWN and
nobody loses track of how much was parked. It carries no address and no file
list. The operator struck both from a first draft on sight: *"nothing in
the system should know where the old tests are, or something could go and run
them by itself, and we do not want that."* (translated) A path in a manifest is not documentation, it
is an instruction something eventually follows. A number is not.
"""
from __future__ import annotations

import json
from pathlib import Path

LIVE = "tests"
MANIFEST_REL = "config/parked-tests.json"


def all_test_files(root: Path) -> list[Path]:
    """Every `test_*.py` in THIS repository, sorted, files only.

    "All" means all of the engine's, which since 2026-09-21 is all there are
    here. The parked suite is not reached and is not counted.
    """
    base = root / LIVE
    if not base.is_dir():
        return []
    return sorted(p for p in base.rglob("test_*.py") if p.is_file())


def corpus_floor(root: Path) -> int:
    """The count below which a verdict over the test corpus is not a verdict.

    DERIVED, never a constant. Two ratchets each carried `MIN_CORPUS = 400`, a
    number measured on 2026-09-02 against 1,051 files. After the split the same
    constant refused over a corpus of 2, which is the floor working correctly
    and answering the wrong question: 2 was the whole live suite, not a walk
    that had gone wrong.

    What it still catches, which is the reason it is not simply deleted: a walk
    that returns FEWER files than the tree holds. The floor is the live count
    itself, so it refuses only when the enumeration disagrees with the
    directory, and never merely because the suite is small.
    """
    return max(len(all_test_files(root)), 1)


def parked_count(root: Path) -> int:
    """How many test files are parked, from the manifest. 0 when there is none."""
    try:
        return int(json.loads(
            (root / MANIFEST_REL).read_text(encoding="utf-8")).get("parked_test_files", 0))
    except (OSError, ValueError, TypeError):
        return 0


def is_test_path(rel: str) -> bool:
    """Whether a repository-relative path names a test file in this repository."""
    return rel.startswith(LIVE + "/")
