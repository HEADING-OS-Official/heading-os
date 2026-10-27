"""The usage ledger of every model call that goes through the local proxy.

CLIProxyAPI keeps no per-call token statistics of its own (removed upstream;
its request log carries status, latency and path only), so the one place that
sees every call AND its `usage` object is `call_model` in
`scripts/utils/proxy_transport.py`. It appends one row here per attempt, and
`scripts/proxy-usage.py` reads them back into a report and a dashboard.

The ledger is the registry name `proxy-usage-ledger`, one JSON object per line:

    ts                 UTC, ISO 8601, seconds
    call_id            groups the attempts of one `call_model` invocation
    attempt            1 for the first request of that invocation, then 2, 3...
    retry              attempt > 1; every retry is a request that costs tokens
    model              the model the caller asked for (may be an alias)
    served             `model` of the response (or of an error reply's JSON body),
                       what the proxy routed to; None if absent
    caller             basename of argv[0]: the script whose process made the call
    prompt_tokens, completion_tokens, reasoning_tokens, total_tokens
                       from `usage` of the response or of an error reply's body;
                       None where neither carried one
    cached_tokens      `usage.prompt_tokens_details.cached_tokens`: the part of
                       prompt_tokens read from the provider's cache; None where
                       absent, and in every row written before the field existed (2026-10-25)
    http_status        the status code of a non-2xx reply the SDK raised on; None
                       for a successful reply and for an error with no HTTP reply
    duration_ms        wall time of the request
    outcome            ok | empty | length | content_filter | error:<ExceptionClass>

Never any prompt or answer text: the row says THAT a call happened and what it
cost in tokens, never what was said.

Several processes (HELM, every yard, the dashboard timer's reader) touch the
ledger at once. `O_APPEND` makes each single `os.write` land at the end, but a
row written in two parts after a short write could be split by another
process's row, leaving two lines the reader skips. So the whole write loop runs
under an exclusive `fcntl.flock` on the same descriptor, released in `finally`.
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from scripts.utils.data_path import data_path
from scripts.utils.jsonl_lines import jsonl_lines

def ledger_path() -> Path:
    """The ledger file: the registry name `proxy-usage-ledger`."""
    return data_path("proxy-usage-ledger")


def new_call_id() -> str:
    return uuid.uuid4().hex[:12]


def caller_name() -> str:
    """The script whose process made the call, as argv[0] names it."""
    argv0 = sys.argv[0] if sys.argv and sys.argv[0] else ""
    name = Path(argv0).name
    return name if name and name not in ("-c", "-") else "python"


def _field(obj, name):
    """`name` of an SDK object or of a parsed JSON dict; None when absent."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _usage_fields(resp) -> dict:
    """Token counts from an OpenAI-shaped response or error body; None each one absent."""
    usage = _field(resp, "usage")

    def _int(obj, attr):
        value = _field(obj, attr)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    details = _field(usage, "completion_tokens_details")
    return {
        "prompt_tokens": _int(usage, "prompt_tokens"),
        "completion_tokens": _int(usage, "completion_tokens"),
        "reasoning_tokens": _int(details, "reasoning_tokens"),
        "total_tokens": _int(usage, "total_tokens"),
        "cached_tokens": _int(_field(usage, "prompt_tokens_details"), "cached_tokens"),
    }


def make_row(*, call_id: str, attempt: int, model: str, resp, duration_ms: int,
              outcome: str, http_status: int | None = None) -> dict:
    """`resp` is the SDK response object, or the parsed JSON body of an error reply."""
    served = _field(resp, "model")
    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "call_id": call_id,
        "attempt": attempt,
        "retry": attempt > 1,
        "model": model,
        "served": served if isinstance(served, str) else None,
        "caller": caller_name(),
        **_usage_fields(resp),
        "http_status": http_status,
        "duration_ms": duration_ms,
        "outcome": outcome,
    }


def append_row(row: dict, path: Path | None = None) -> None:
    """Append one row as one line, under an exclusive lock. Raises OSError on a
    failed or short write."""
    path = path or ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            written = 0
            while written < len(data):
                sent = os.write(fd, data[written:])
                if sent <= 0:
                    raise OSError(f"short write to {path}: {written} of {len(data)} bytes")
                written += sent
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def read_rows(path: Path | None = None) -> tuple[list[dict], int]:
    """Every readable row, in write order, and the count of lines skipped.

    No file means no rows. An undecodable or non-JSON line is skipped and
    COUNTED, so a report can say how many it left out.
    """
    path = path or ledger_path()
    if not path.exists():
        return [], 0
    rows: list[dict] = []
    skipped = 0
    for line in jsonl_lines(path):
        if line is None:
            skipped += 1
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if isinstance(row, dict) and isinstance(row.get("ts"), str):
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped
