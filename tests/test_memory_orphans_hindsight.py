"""GUARANTEE: an auto-memory record is an orphan exactly when MEMORY.md does not link it AND the Hindsight bank does not hold it, and a bank that did not answer convicts nobody.

Since 2026-10-04 (HOS-198) the second reachability source is the Hindsight bank
`main`, asked by `memory_health.hindsight_record_names()` through one filtered,
read-only listing; until that day it was a knowledge graph. This file drives
`compute_memory_defects`, the function both the weekly gate
(`scripts/memory-hygiene.py`, run by `memory-hygiene.timer`) and `/prime`'s
memory panel call, with only the HTTP GET stubbed at its seam
(`_hindsight_get`) and the settings at theirs (`_hindsight_conn`). The stub
answers like the live server did on 2026-10-04: `q` filters by substring of
the id, `limit` and `offset` page, and each page carries `items` and `total`.

Every name and address below is invented. Nothing here reaches a real server:
the conftest pins `HEADING_OS_DATA` to `tmp_path` and refuses non-local
egress, and the stub never opens a socket.

RED ON THE PREVIOUS CODE, MEASURED 2026-10-04: run against `git show
HEAD:scripts/utils/memory_health.py` extracted outside the checkout, the first
three functions fail (the old code never asks the bank, so nothing is
evaluated and no orphan is named), and the fourth passes, since an unanswered
store already meant "not evaluated" there.
"""
from __future__ import annotations

import importlib.util
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

from scripts.utils import memory_health

ROOT = Path(__file__).resolve().parent.parent


def _bank_ids() -> list[str]:
    """The invented bank: what the sync would have sent, plus ids that must not count."""
    return [
        "auto-memory/held-whole.md",
        "auto-memory/held-in-parts.md#part-01",
        "auto-memory/held-in-parts.md#part-02",
        "auto-memory/linked-and-held.md",
        # Must NOT make `lost.md` reachable: a subdirectory, a prefix in the
        # middle of a longer path, and a record of another directory.
        "auto-memory/archive/lost.md",
        "notes/auto-memory/lost.md",
        "notes/lost.md",
    ]


def _corpus(tmp_path: Path) -> Path:
    memory_dir = tmp_path / "auto-memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text(
        "# Index\n\n- [linked](linked-only.md)\n- [both](linked-and-held.md)\n",
        encoding="utf-8")
    for name in ("linked-only.md", "linked-and-held.md", "held-whole.md",
                 "held-in-parts.md", "lost.md"):
        (memory_dir / name).write_text(f"# {name}\n\nAn invented record.\n",
                                       encoding="utf-8")
    return memory_dir


def _stub_bank(monkeypatch, ids, *, page=2, fail=None, extra_total=0) -> list[str]:
    """Stub settings and the GET; return the list of URLs the code asked for."""
    asked: list[str] = []

    def get(url, key, timeout):
        asked.append(url)
        if fail is not None:
            raise fail
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        q = query.get("q", [""])[0]
        offset = int(query.get("offset", ["0"])[0])
        matched = [i for i in ids if q in i]
        items = [{"id": i} for i in matched[offset:offset + page]]
        return {"items": items, "total": len(matched) + extra_total,
                "limit": page, "offset": offset}

    monkeypatch.setattr(memory_health, "_hindsight_conn",
                        lambda: ("http://127.0.0.1:1", "main", "invented-key"),
                        raising=False)
    monkeypatch.setattr(memory_health, "_hindsight_get", get, raising=False)
    return asked


def test_a_record_neither_linked_nor_held_is_an_orphan(tmp_path, monkeypatch):
    """The record the bank does not hold and the index does not link is named."""
    asked = _stub_bank(monkeypatch, _bank_ids())
    result = memory_health.compute_memory_defects(_corpus(tmp_path))
    assert result["orphans_evaluated"] is True
    assert result["orphans"] == ["lost.md"]
    assert asked, "the bank was never asked"
    assert all("q=auto-memory%2F" in url for url in asked)


def test_a_record_held_by_the_bank_is_not_an_orphan(tmp_path, monkeypatch):
    """Unlinked but held, whole or in parts: reachable, so not an orphan."""
    _stub_bank(monkeypatch, _bank_ids())
    result = memory_health.compute_memory_defects(_corpus(tmp_path))
    assert result["orphans_evaluated"] is True
    assert not {"held-whole.md", "held-in-parts.md"} & set(result["orphans"])
    assert result["hindsight_named_records"] == 3
    assert result["hindsight_corpus_records"] == 5


def test_a_record_linked_from_the_index_is_not_an_orphan(tmp_path, monkeypatch):
    """Linked and absent from the bank: the index alone reaches it."""
    _stub_bank(monkeypatch, _bank_ids())
    result = memory_health.compute_memory_defects(_corpus(tmp_path))
    assert result["orphans_evaluated"] is True
    assert not {"linked-only.md", "linked-and-held.md"} & set(result["orphans"])


@pytest.mark.parametrize("bank", ["refused", "under-floor", "incomplete"])
def test_a_bank_that_did_not_answer_evaluates_nothing(tmp_path, monkeypatch, bank):
    """Unreachable, under half the corpus, or a listing short of its own total:
    NOT EVALUATED, never a pass.

    The weekly gate's own `coverage()` must then leave `orphans` out of the
    categories it evaluated, so the run says "not evaluated" rather than
    "0 orphans".
    """
    if bank == "refused":
        _stub_bank(monkeypatch, _bank_ids(),
                   fail=urllib.error.URLError("connection refused"))
    elif bank == "under-floor":
        _stub_bank(monkeypatch, ["auto-memory/held-whole.md"])
    else:
        _stub_bank(monkeypatch, _bank_ids(), extra_total=2)
    memory_dir = _corpus(tmp_path)
    result = memory_health.compute_memory_defects(memory_dir)
    assert result["orphans_evaluated"] is False
    assert result["orphans"] == []

    spec = importlib.util.spec_from_file_location(
        "_memory_hygiene_under_test", ROOT / "scripts" / "memory-hygiene.py")
    hygiene = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hygiene)
    cov = hygiene.coverage(memory_dir, result, brain_ok=True)
    assert "orphans" in cov["categories_not_evaluated"]
