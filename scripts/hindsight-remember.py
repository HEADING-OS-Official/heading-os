#!/usr/bin/env python3
"""Remember one thing straight into the Hindsight bank, and read it back.

    hindsight-remember.py --id <slug> "<the memory, in full sentences>" [--tag T ...] [--context C] [--replace]
    hindsight-remember.py --id <slug> --file NOTE.md [--tag T ...]
    hindsight-remember.py self-test        # invented bank on a loopback port; no real server

Only for a memory that has NO file behind it. A memory to keep is otherwise a file in
`auto-memory/` with `written_by: operator` or `helm`, which the file sync mirrors into
Hindsight (M015.1 in the operator's policy, reworded 2026-10-14 under HOS-244; it replaced
the HOS-203 decision of 2026-10-05 that sent "remember this" here instead of to a file).
This command touches no auto-memory file.

The document id is `memory:<slug>`. A meaningful slug is required rather than derived,
because the id is how the memory is found, replaced and talked about later. The `memory:`
prefix is not a repository path, which is what keeps the memory out of everything
`hindsight-sync.py` deletes: its mirror only reaches ids it sent from files, and its
`cleanup` never plans an id of the `<word>:` shape. It is also what the daily snapshot
(`hindsight-snapshot.py`) copies to disk, since no file stands behind it.

Tags: `memory` and `source:remember`, plus every `--tag`. An id that already exists is
refused unless `--replace` is given, which replaces its text, tags and extracted facts.

The retain is asynchronous on the server; this waits for the operation (up to `--wait`
seconds), then reads the document back and confirms that its text and tags are what was
sent and how many memory units Hindsight extracted. Anything short of that exits non-zero
and says which step did not happen. Settings are `hindsight-sync.py`'s
(`HINDSIGHT_SYNC_API_URL`, `HINDSIGHT_SYNC_BANK`, `HINDSIGHT_SYNC_KEY_FILE`).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.utils.loopback_json_server import serve as serve_loopback  # noqa: E402

PREFIX = "memory:"
BASE_TAGS = ("memory", "source:remember")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{2,79}$")
DONE = {"completed", "succeeded", "success", "done"}
FAILED = {"failed", "cancelled", "canceled", "error", "not_found"}

_SYNC = None


def sync_module():
    """`scripts/hindsight-sync.py`: its settings and its HTTP call, used as they are."""
    global _SYNC
    if _SYNC is None:
        spec = importlib.util.spec_from_file_location("hindsight_sync_for_remember",
                                                      ROOT / "scripts" / "hindsight-sync.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SYNC = mod
    return _SYNC


class NotRemembered(RuntimeError):
    """A step did not happen; the message names it."""


def _doc_url(api: str, bank: str, doc_id: str) -> str:
    return f"{api}/v1/default/banks/{bank}/documents/{urllib.parse.quote(doc_id, safe='')}"


def existing(api: str, bank: str, key: str, doc_id: str) -> dict | None:
    try:
        return sync_module()._api("GET", _doc_url(api, bank, doc_id), key)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def remember(api: str, bank: str, key: str, slug: str, text: str, tags: list[str], context: str = "",
             replace: bool = False, wait: float = 600.0, poll: float = 2.0) -> dict:
    if not SLUG.match(slug):
        raise NotRemembered(f"the id {slug!r} is not a slug: lowercase letters, digits and hyphens, 3 to 80")
    if not text.strip():
        raise NotRemembered("nothing to remember: the text is empty")
    doc_id = PREFIX + slug
    if existing(api, bank, key, doc_id) is not None and not replace:
        raise NotRemembered(f"{doc_id} already exists; pass --replace to replace it")
    all_tags = list(dict.fromkeys([*BASE_TAGS, *tags]))
    item = {"content": text, "document_id": doc_id, "tags": all_tags,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "metadata": {"source": "hindsight-remember"}, "update_mode": "replace"}
    if context:
        item["context"] = context
    sync = sync_module()
    sent = sync._api("POST", f"{api}/v1/default/banks/{bank}/memories", key,
                     {"items": [item], "async": True, "operation_id": str(uuid.uuid4())})
    op = sent.get("operation_id")
    if not op:
        raise NotRemembered(f"the server accepted no operation for {doc_id}")
    deadline = time.monotonic() + wait
    status = ""
    while time.monotonic() < deadline:
        body = sync._api("GET", f"{api}/v1/default/banks/{bank}/operations/{op}", key)
        status = str(body.get("status") or "").lower()
        if status in DONE:
            break
        if status in FAILED:
            raise NotRemembered(f"operation {op} for {doc_id} ended {status}: {body.get('error_message')}")
        time.sleep(poll)
    else:
        raise NotRemembered(f"operation {op} for {doc_id} still {status or 'unknown'} after {wait:.0f}s; "
                            "it may still land, read it back later with `hindsight document get`")
    doc = existing(api, bank, key, doc_id)
    if doc is None:
        raise NotRemembered(f"operation {op} completed but {doc_id} is not in the bank")
    if doc.get("original_text") != text:
        raise NotRemembered(f"{doc_id} reads back with a different text")
    missing = [t for t in all_tags if t not in (doc.get("tags") or [])]
    if missing:
        raise NotRemembered(f"{doc_id} reads back without the tags {missing}")
    return {"id": doc_id, "operation_id": op, "memory_units": doc.get("memory_unit_count"),
            "tags": doc.get("tags"), "bank": bank}


def cmd_remember(args) -> int:
    text = Path(args.file).read_text(encoding="utf-8") if args.file else (args.text or "")
    try:
        api, bank, key = sync_module()._conn()
        out = remember(api, bank, key, args.id, text, args.tag or [], args.context or "",
                       args.replace, args.wait)
    except (NotRemembered, urllib.error.URLError, OSError, ValueError) as exc:
        print(f"not remembered: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(out, ensure_ascii=False) if args.json else
          f"remembered {out['id']} in bank {out['bank']}: read back, {out['memory_units']} memory unit(s), "
          f"tags {', '.join(out['tags'])}")
    if not out["memory_units"]:
        # MEASURED 2026-10-05: a text that states no fact is stored and read back whole, and
        # Hindsight extracts nothing from it, so recall can never surface it.
        print(f"warning: Hindsight extracted no facts from {out['id']}, so recall will not find it; "
              "restate it as plain factual sentences and run again with --replace", file=sys.stderr)
        return 2
    return 0


# ============================================================
# Self-test: an invented bank on a loopback port; no real server
# ============================================================


class _Bank:
    def __init__(self, finish: str = "completed", drop_tag: bool = False):
        self.docs: dict[str, dict] = {}
        self.ops: dict[str, dict] = {}
        self.finish, self.drop_tag = finish, drop_tag

    def answer(self, method: str, path: str, query: dict, body: dict | None) -> tuple[int, dict]:
        base = "/v1/default/banks/inv/"
        rest = path[len(base):] if path.startswith(base) else ""
        if method == "POST" and rest == "memories":
            item = body["items"][0]
            tags = item["tags"][:-1] if self.drop_tag else item["tags"]
            self.ops[body["operation_id"]] = {"status": self.finish}
            if self.finish == "completed":
                self.docs[item["document_id"]] = {"id": item["document_id"], "original_text": item["content"],
                                                  "tags": tags, "memory_unit_count": 2}
            return 200, {"success": True, "operation_id": body["operation_id"], "async": True}
        if method == "GET" and rest.startswith("operations/"):
            op = self.ops.get(rest.split("/", 1)[1])
            return (404, {}) if op is None else (200, op)
        if method == "GET" and rest.startswith("documents/"):
            d = self.docs.get(urllib.parse.unquote(rest.split("/", 1)[1]))
            return (404, {"detail": "not found"}) if d is None else (200, d)
        return 404, {}


def _serve(bank: _Bank):
    return serve_loopback(bank.answer)


def _raises(fn, needle: str) -> None:
    try:
        fn()
    except NotRemembered as exc:
        assert needle in str(exc), exc
    else:
        raise AssertionError(f"expected a refusal naming {needle!r}")


def _st_remembers_reads_back_and_refuses_a_second_write_without_replace() -> None:
    bank = _Bank()
    with _serve(bank) as api:
        out = remember(api, "inv", "k", "invented-fact", "An invented fact.", ["topic:invented"], poll=0.01)
        assert out["id"] == "memory:invented-fact" and out["memory_units"] == 2, out
        assert set(BASE_TAGS) | {"topic:invented"} <= set(out["tags"]), out
        _raises(lambda: remember(api, "inv", "k", "invented-fact", "Changed.", [], poll=0.01), "already exists")
        remember(api, "inv", "k", "invented-fact", "Changed.", [], replace=True, poll=0.01)
        assert bank.docs["memory:invented-fact"]["original_text"] == "Changed."


def _st_refuses_a_bad_id_and_reports_every_unconfirmed_step() -> None:
    with _serve(_Bank()) as api:
        _raises(lambda: remember(api, "inv", "k", "Bad Id", "x", [], poll=0.01), "not a slug")
        _raises(lambda: remember(api, "inv", "k", "empty-one", "  ", [], poll=0.01), "empty")
    with _serve(_Bank(finish="failed")) as api:
        _raises(lambda: remember(api, "inv", "k", "fails-here", "x", [], poll=0.01), "ended failed")
    with _serve(_Bank(finish="pending")) as api:
        _raises(lambda: remember(api, "inv", "k", "slow-one", "x", [], wait=0.2, poll=0.05), "still pending")
    with _serve(_Bank(drop_tag=True)) as api:
        _raises(lambda: remember(api, "inv", "k", "tagless", "x", ["t:one"], poll=0.01), "without the tags")


SELF_TESTS = [
    _st_remembers_reads_back_and_refuses_a_second_write_without_replace,
    _st_refuses_a_bad_id_and_reports_every_unconfirmed_step,
]


def cmd_self_test(_args) -> int:
    failed = 0
    for t in SELF_TESTS:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001 - every scenario reports, none stops the rest
            failed += 1
            print(f"FAIL {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"{len(SELF_TESTS) - failed}/{len(SELF_TESTS)} passed")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["self-test"]:
        return cmd_self_test(None)
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[1], formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("text", nargs="?", help="the memory itself, in full sentences")
    p.add_argument("--id", required=True, help="slug; the document id becomes memory:<slug>")
    p.add_argument("--file", help="read the memory from this file instead")
    p.add_argument("--tag", action="append", help="an extra tag (repeatable)")
    p.add_argument("--context", help="one line on where the memory comes from")
    p.add_argument("--replace", action="store_true", help="replace an existing memory with this id")
    p.add_argument("--wait", type=float, default=600.0, help="seconds to wait for the server to finish")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    if bool(args.text) == bool(args.file):
        p.error("give the memory as text or as --file, exactly one")
    return cmd_remember(args)


if __name__ == "__main__":
    sys.exit(main())
