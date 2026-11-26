"""The commit gate lot-map-rules-agree refuses when the two copies of the station rules drift.

The rules choosing a station and a label for a tool call exist in
scripts/lot-map.py and in the lot-map mod's stations.ts. The gate runs
scripts/check-lot-map-rules.py over a shared case corpus whenever either copy
changes. This is the one test of that gate, the exception the operator named for
HOS-414 and listed in config/test-exceptions.yaml (2026-11-23).

It needs Node 24 on PATH, as the gate does, and fails rather than skips without
it; CI excludes it by its marker, because the runner's Node is not pinned.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.requires_node

ROOT = Path(__file__).resolve().parent.parent
CHECKER = "scripts/check-lot-map-rules.py"
CASES = ROOT / "scripts" / "claude-mods" / "lot-map" / "station-cases.json"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([".venv/bin/python", CHECKER, *args], cwd=ROOT,
                          capture_output=True, text=True, timeout=120, check=False)


def test_the_two_copies_agree_on_every_case_of_the_corpus():
    corpus = json.loads(CASES.read_text(encoding="utf-8"))
    # MEASURED 2026-11-23: 61 cases committed with HOS-414 item 2.
    assert len(corpus) >= 61
    proc = _run()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    m = re.search(r"lot-map rules agree: (\d+) cases", proc.stdout)
    assert m, proc.stdout
    assert int(m.group(1)) == len(corpus)


def test_a_copy_that_drifted_is_refused_and_the_case_named(tmp_path):
    original = (ROOT / "scripts" / "lot-map.py").read_text(encoding="utf-8")
    old = 'NETWORK_COMMANDS = {"curl", "wget",'
    assert original.count(old) == 1
    drifted = original.replace(old, 'NETWORK_COMMANDS = {"wget",')
    assert drifted != original
    copy = tmp_path / "lot-map.py"
    copy.write_text(drifted, encoding="utf-8")
    proc = _run("--lot-map-py", str(copy))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "curl https://example.invalid/x" in proc.stdout


def test_an_empty_corpus_never_passes(tmp_path):
    empty = tmp_path / "cases.json"
    empty.write_text("[]\n", encoding="utf-8")
    proc = _run("--cases", str(empty))
    assert proc.returncode == 1, proc.stdout + proc.stderr
