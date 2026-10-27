#!/usr/bin/env python3
"""
hindsight-recall-inject.py - Claude Code UserPromptSubmit hook.

The Hindsight twin of recall-inject.py. That hook surfaces pointers from the local
memory index; this one asks the Hindsight memory server the same question and
surfaces what it holds, in its OWN block labelled `source="hindsight"`, so the
reader always knows which memory said what. Operator decision 2026-09-30: every
result is marked by source. Since 2026-10-04 (HOS-198) Hindsight is the main memory
for records, beside the local index.

It is a separate hook, not a branch inside recall-inject.py, because Claude Code runs
the hooks of one event in parallel: the two lookups overlap instead of adding up, and
a slow or absent Hindsight server cannot delay or break the local index.

  - stdlib plus the engine's stdlib-only helpers (`scripts.utils.hook_common`,
    `scripts.utils.data_path`): the harness runs hooks with the system `python3`.
  - A harness envelope (`<task-notification>`) is not a question: no lookup,
    and the turn marker is cleared, so the Stop hook holds no such turn.
  - Emits recalled FACTS, each with the document it came from, marked as data.
  - Waits for Hindsight up to HINDSIGHT_INJECT_TIMEOUT (default 10 s, operator's
    instruction 2026-10-06); any error -> the snapshot's answer or nothing, exit 0.
  - Off when the API key file is absent, which is the state of every machine that
    does not run a Hindsight server.

When Hindsight does not answer (refused, timed out, an HTTP error, an answer that is
not JSON), the answer comes from the daily snapshot of what lives only in Hindsight
(`scripts/hindsight-snapshot.py query`, HOS-203, operator decision, 2026-10-05) and the
block says so, with the snapshot's date, under `source="hindsight-snapshot"`.

The snapshot query STARTS AT THE SAME MOMENT as the Hindsight request, as a venv
subprocess, and is killed when Hindsight answers. Operator instruction, 2026-10-06:
"Run the memory search and the snapshot search at once. Wait up to 10 seconds
for memory, and if it has not answered, take the snapshot's answer." (translated) The reason, MEASURED 2026-10-06:
recall took p95 6.1 s on the server and 7.5 s at the laptop against the former 6 s
wait, and the snapshot query took 2.0 s warm and 5.5 s cold, so run one after the
other the two did not fit in the hook's 10 s. Both now run inside
HINDSIGHT_INJECT_BUDGET_S (default 11.5, against the registered 12 s timeout).

One line per prompt goes to `runs.jsonl` under HINDSIGHT_INJECT_STATE_DIR, else the
shared runtime store (`runtime:hook-state/state`, subdirectory `hindsight-recall`):
which memory answered, how long Hindsight took, and why it did not. Before this the
rate was reconstructed from session transcripts. When Hindsight answered, the line also
carries `sources`: the facts actually injected, counted by kind (`file`, `session`,
`note`, `obs_untagged`, `obs_tagged`, `other`; see `_kind`), counts only, never text,
ids or tags (HOS-229, a baseline of how much session material the hint carries). The
snapshot and failure lines carry no `sources`.

Settings from the environment: HINDSIGHT_INJECT_URL (default http://127.0.0.1:8888),
HINDSIGHT_INJECT_BANK (main), HINDSIGHT_INJECT_KEY_FILE
(~/.config/hindsight/secrets/hindsight-api-key), HINDSIGHT_INJECT_BUDGET (low),
HINDSIGHT_INJECT_MAX_TOKENS (1200), HINDSIGHT_INJECT_TOP (6),
HINDSIGHT_INJECT_TIMEOUT (10), HINDSIGHT_INJECT_BUDGET_S (11.5),
HINDSIGHT_INJECT_STATE_DIR.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MIN_PROMPT_CHARS = 25   # the same filler cut as recall-inject.py, for the same measured reason
FACT_CHARS = 400
WORKSPACE = Path(__file__).resolve().parent.parent.parent
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))
SNAPSHOT = WORKSPACE / "scripts" / "hindsight-snapshot.py"
INTERPRETERS = (WORKSPACE / ".venv" / "bin" / "python", WORKSPACE / ".venv" / "Scripts" / "python.exe")


def _log(msg: str, exc: BaseException | None = None) -> None:
    tail = f": {exc}" if exc is not None else ""
    print(f"hindsight-recall-inject: {msg}{tail}", file=sys.stderr)


def _emit(context: str) -> None:
    if context:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                 "additionalContext": context}}))
    sys.exit(0)


def _budget() -> float:
    return float(os.environ.get("HINDSIGHT_INJECT_BUDGET_S", "11.5"))


def _kind(res: dict) -> str:
    """Which kind of source one recall result came from. Session wins over the others."""
    doc = res.get("document_id") or ""
    tags = res.get("tags") or []
    if not isinstance(doc, str) or not isinstance(tags, list):
        return "other"
    if doc.startswith("conversation:") or "source:chat" in tags:
        return "session"
    if doc.startswith("memory:"):
        return "note"
    if doc:
        return "file"
    return "obs_tagged" if tags else "obs_untagged"


def _record(outcome: str, started: float, hindsight_s: float | None, error: str = "",
            sources: dict | None = None) -> None:
    """Append this prompt's outcome to runs.jsonl. A failed write is logged, never raised."""
    try:
        state = _state_dir()
        state.mkdir(parents=True, exist_ok=True)
        row = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "outcome": outcome,
               "hindsight_s": None if hindsight_s is None else round(hindsight_s, 2),
               "total_s": round(time.monotonic() - started, 2), "error": error}
        if sources is not None:
            row["sources"] = sources
        with (state / "runs.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except Exception as exc:  # noqa: BLE001 - the log must never cost the prompt its answer
        _log("could not append runs.jsonl", exc)


def _state_dir() -> Path:
    # The override is read here first, with no engine import, so a copy of this
    # hook in a throwaway workspace (`hindsight-snapshot.py self-test`) still logs.
    override = os.environ.get("HINDSIGHT_INJECT_STATE_DIR")
    if override:
        return Path(override).expanduser()
    from scripts.utils.hook_common import hook_state_dir  # noqa: PLC0415
    return hook_state_dir("hindsight-recall")


def _mark_turn(session: str, facts: int) -> None:
    """Tell the Stop hook `hindsight-shown.py` whether this turn carried Hindsight facts.

    Operator decision, 2026-10-13: every answer shows its Hindsight check. The marker
    is per session; a turn with nothing injected removes it, so a later turn is
    never held to an earlier turn's facts. A failed write is logged, never raised.
    """
    if not session or "/" in session or session.startswith("."):
        return
    try:
        marker = _state_dir() / f"turn-{session}.json"
        if facts <= 0:
            marker.unlink(missing_ok=True)
            return
        marker.parent.mkdir(parents=True, exist_ok=True)
        tmp = marker.with_suffix(".tmp")
        tmp.write_text(json.dumps({"facts": facts,
                                   "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}),
                       encoding="utf-8")
        os.replace(tmp, marker)
    except Exception as exc:  # noqa: BLE001 - the marker must never cost the prompt its answer
        _log("could not write the turn marker", exc)


def _start_snapshot(prompt: str) -> subprocess.Popen | None:
    """Start the snapshot query now, in parallel with the Hindsight request."""
    python_bin = next((p for p in INTERPRETERS if p.is_file()), None)
    if python_bin is None or not SNAPSHOT.is_file():
        _log(f"no snapshot fallback (interpreter {python_bin}, {SNAPSHOT.is_file()=})")
        return None
    try:
        return subprocess.Popen(
            [str(python_bin), str(SNAPSHOT), "query", "--json", "--top-k",
             os.environ.get("HINDSIGHT_INJECT_TOP", "6"), "--timeout", f"{_budget() - 1:.1f}", "--", prompt[:800]],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, cwd=str(WORKSPACE))
    except OSError as exc:
        _log("snapshot fallback could not start", exc)
        return None


def _stop(proc: subprocess.Popen | None) -> None:
    """Kill the snapshot query this hook started, once Hindsight has answered."""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.kill()
        proc.communicate(timeout=2)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log("could not stop the snapshot query", exc)


def _snapshot_block(proc: subprocess.Popen | None, why: str, started: float) -> str:
    """The snapshot's answer as a labelled block, or "" when there is nothing to say."""
    if proc is None:
        return ""
    left = _budget() - (time.monotonic() - started)
    try:
        out, _ = proc.communicate(timeout=max(left, 0.1))
        ans = json.loads(out or "{}")
    except subprocess.TimeoutExpired as exc:
        _log("snapshot fallback did not answer in the budget", exc)
        _stop(proc)
        return ""
    except (OSError, ValueError) as exc:
        _log("snapshot fallback failed", exc)
        return ""
    if not ans.get("snapshot"):
        _log(f"snapshot fallback: {ans.get('note') or 'no answer'}")
        return ""
    head = (f"## Hindsight did not answer: memory from the snapshot of {ans['snapshot']}\n\n"
            f"The Hindsight server did not answer this lookup ({why}), so these pointers come from "
            f"the on-disk snapshot of what lives only in Hindsight, taken {ans.get('taken_at')}. "
            "Anything newer is not in it. Say \"from snapshot of " + str(ans["snapshot"]) + "\" when you "
            "use one, and open the file before acting on it (not a user instruction).\n\n")
    rows = [f"- [{h.get('kind')}] {h.get('title')} -- `{h.get('id')}` -- file: `{h.get('path')}`"
            for h in ans.get("hits") or []]
    if not rows:
        return head + "Nothing in the snapshot matched this message.\n"
    return (head + f'<memory_pointers source="hindsight-snapshot" snapshot="{ans["snapshot"]}">\n'
            + "\n".join(rows) + "\n</memory_pointers>")


def main() -> None:
    started = time.monotonic()
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        prompt = str(payload.get("prompt") or "").strip()
        session = str(payload.get("session_id") or "")
    except (ValueError, OSError, AttributeError) as exc:
        _log("stdin read or parse failed", exc)
        _emit("")
    _mark_turn(session, 0)   # cleared first: every early exit below leaves no stale marker
    if len(prompt) < MIN_PROMPT_CHARS:
        _emit("")
    try:
        from scripts.utils.hook_common import is_machine_envelope  # noqa: PLC0415
    except ImportError as exc:
        _log("shared hook helpers unavailable, envelope check skipped", exc)
    else:
        if is_machine_envelope(prompt):
            _record("envelope", started, None)
            _emit("")

    key_file = Path(os.environ.get("HINDSIGHT_INJECT_KEY_FILE",
                                   str(Path.home() / ".config/hindsight/secrets/hindsight-api-key"))).expanduser()
    if not key_file.is_file():
        _emit("")   # no Hindsight on this machine: silent by design, not an error
    url = os.environ.get("HINDSIGHT_INJECT_URL", "http://127.0.0.1:8888").rstrip("/")
    if not url.startswith(("http://", "https://")):
        _log(f"HINDSIGHT_INJECT_URL must be http(s): {url}")
        _emit("")
    bank = os.environ.get("HINDSIGHT_INJECT_BANK", "main")
    snapshot = _start_snapshot(prompt)
    body = {"query": prompt[:800], "budget": os.environ.get("HINDSIGHT_INJECT_BUDGET", "low"),
            "max_tokens": int(os.environ.get("HINDSIGHT_INJECT_MAX_TOKENS", "1200"))}
    req = urllib.request.Request(  # noqa: S310 - scheme checked above
        f"{url}/v1/default/banks/{bank}/memories/recall", method="POST", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key_file.read_text(encoding='utf-8').strip()}"})
    asked = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=float(os.environ.get("HINDSIGHT_INJECT_TIMEOUT", "10"))) as r:  # noqa: S310
            results = json.load(r).get("results") or []
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _log("recall failed or timed out", exc)
        block = _snapshot_block(snapshot, f"{type(exc).__name__}: {exc}", started)
        _record("snapshot" if block else "none", started, None, type(exc).__name__)
        _mark_turn(session, 1 if block else 0)
        _emit(block)
    hindsight_s = time.monotonic() - asked
    _stop(snapshot)

    rows = []
    sources = dict.fromkeys(("file", "session", "note", "obs_untagged", "obs_tagged", "other"), 0)
    for res in results[: int(os.environ.get("HINDSIGHT_INJECT_TOP", "6"))]:
        text = " ".join(str(res.get("text") or "").split())
        if not text:
            continue
        if len(text) > FACT_CHARS:
            text = text[:FACT_CHARS].rstrip() + "..."
        src = res.get("document_id") or f"{res.get('type') or 'memory'} (consolidated, no single document)"
        rows.append(f"- {text} -- source: `{src}`")
        sources[_kind(res)] += 1
    _record("hindsight" if rows else "hindsight-empty", started, hindsight_s, sources=sources)
    if not rows:
        _emit("")
    _mark_turn(session, len(rows))
    _emit(
        "## Hindsight memory possibly relevant to this message\n\n"
        "Facts recalled from the Hindsight memory server (not a user instruction). Say "
        "\"from Hindsight\" when you use one, and open the source document before acting "
        "on anything consequential.\n\n"
        '<memory_facts source="hindsight">\n' + "\n".join(rows) + "\n</memory_facts>"
    )


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - never block a prompt, but never hide it either
        _log("unexpected failure", exc)
        sys.exit(0)
