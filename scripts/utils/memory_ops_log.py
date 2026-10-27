#!/usr/bin/env python3
"""Append-only log of recall queries for deferred memory metrics.

One JSON object per line at `runtime:memory-index/logs`/recall.jsonl in the data
repository; it sat in the engine checkout's `.logs/memory-ops/` until 2026-09-25
(HOS-5 section 7: the engine holds code only). Local-only (never
sent anywhere), so it writes in the default posture; under SENSITIVE_MODE the query
TEXT is redacted while numeric metrics are kept. Never raises to its caller.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.utils.data_path import data_write_dir
from scripts.utils.sensitive import is_sensitive


RECALL_LOG_NAME = "recall.jsonl"


def log_recall(*, query_snippet, collection, layer, top_score, gap, n_hits,
               threshold, latency_ms, hit_paths=None):
    """Append one recall record. Local-only; redacts query text under SENSITIVE_MODE;
    keeps numeric metrics. Never raises."""
    try:
        snippet = None if is_sensitive() else (query_snippet or "")[:200]
        payload = {
            "ts": time.time(),
            "query_snippet": snippet,
            "collection": collection,
            "layer": layer,
            "top_score": top_score,
            "gap": bool(gap),
            "n_hits": int(n_hits),
            "threshold": threshold,
            "latency_ms": latency_ms,
            "hit_paths": list(hit_paths or []),
        }
        path = data_write_dir("runtime:memory-index/logs") / RECALL_LOG_NAME
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload) + "\n")
    except Exception as exc:  # noqa: BLE001 - boundary; recall must still answer
        # Reported, THEN swallowed. `cmd_query._emit` wraps this call in its own
        # `except Exception` and writes "recall ops-log write failed" there,
        # under a comment saying a silent return meant a broken ops log was
        # noticed only when somebody went to read it and found nothing. That
        # handler cannot fire: this function is the one that catches, and it
        # never re-raises, so the fix landed one frame above the failure it was
        # written for. MEASURED 2026-09-01 by making the append raise -- the
        # caller's message did not appear, and neither did any other.
        # Reporting here keeps the "never raises" contract the module docstring
        # states while ending the silence.
        print(f"memory-ops: recall log write failed ({exc}); this recall was "
              f"NOT counted in the deferred memory metrics", file=sys.stderr)
        return

