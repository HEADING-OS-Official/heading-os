#!/usr/bin/env python3
"""Weekly LLM-fit report - downgrade-audit aggregator.

Track B of the LLM-fit logging project (the `plans` record 2026-05-24-llm-fit-
logging-three-tracks.md). Queries the last N days of Langfuse traces, buckets by
trace name (skill / function), and reports objective signals that a
cheaper model tier could have sufficed.

Surface-level only. ALL judgments are mechanical from the response shape
(output_tokens, has_tool_use, stop_reason, vendor). Never Claude judging
Claude.

Usage:
  python scripts/llm-fit-report.py                # last 7 days
  python scripts/llm-fit-report.py --days 30      # 30-day window
  python scripts/llm-fit-report.py --json         # JSON output for downstream
  python scripts/llm-fit-report.py --no-write     # stdout only (no file)

Output: {YYYY-MM-DD}_llm-fit-report.md under data_path('llm-fit-reports').
Scheduled weekly, Sunday 03:00 in the configured timezone, by the systemd-user
timer `scripts/install-llm-fit-report-timer.sh` installs. The bridge daemon ran
it on that schedule until HOS-7 removed the daemon on 2026-09-21; nothing ran it
from then until that timer is installed.

The input is only as live as the tracing. `scripts/utils/observability.py`
records a trace only when `LANGFUSE_ENABLED` is set to something other than
false/0/no/off/EMPTY and `SENSITIVE_MODE` is explicitly cleared; an empty value
disables it. With tracing off, the report says it found no llm_fallback traces.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKSPACE_ROOT))

from scripts.utils.colors import CYAN, GRAY, GREEN, RESET, YELLOW  # noqa: E402
from scripts.utils.data_path import data_path
from scripts.utils.workspace import get_default_tz_name, load_env

def report_dir() -> Path:
    """Resolved at call time, never at import.

    As a module-level constant this froze the operator's data root the moment
    the module was imported. A caller that set `HEADING_OS_DATA` afterwards (any
    test that imports this module and then repoints the root) redirected
    nothing, and `write_report()` still created
    `<real overlay>/outputs/operations/llm-fit`. `mkdir` is not one of the
    primitives `tests/conftest.py` wraps, so that directory landed silently.

    Through `data_path('llm-fit-reports')`, and through the HEADING OS 2 seam
    from 2026-09-18 until HOS-5 phase 6. Before either, `get_outputs_dir()`
    carried no `_not_vacated` guard, so the bare join raised nothing and simply
    answered a directory that is gone. MEASURED 2026-09-18 on the operator's overlay:
    the pre-move `llm-fit` directory under the bare `outputs` directory did NOT
    exist, while the nine reports written since 2026-05-31 sat at the
    `llm-fit-reports` address. `write_report()` mkdirs
    this path unconditionally, so the next weekly run would have recreated the
    vacated directory and started a second, one-file history beside the real
    one, with nothing said.

    The kind is `("trace", "report")`: MEASURED the same day,
    `manifest.classify` of a pre-move `llm-fit-reports` `<date>_llm-fit-report.md`
    returns `{'family': 'trace', 'leaf': 'report'}` for all nine, the name
    carrying `report` out of `OPERATIONS_REPORT_HINTS`.

    The registry names this address rather than leaving it to the write seam,
    whose `sublocation` join drops the `outputs`, `operations/` tail these records
    carry and so answers `llm-fit` directly under the `("trace", "report")`
    kind's root, which does not exist.
    Nothing falls back now: a public clone or any tree whose registry lacks the
    name raises `UnknownDataPath` rather than resolving as before.
    """
    return data_path('llm-fit-reports')


def _local_today_iso() -> str:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(get_default_tz_name())).strftime("%Y-%m-%d")


def fetch_traces(days: int, page_size: int = 100) -> tuple[list, str | None]:
    """Page through Langfuse traces in the window.

    Returns ``(traces, truncation_reason)``. The reason is None only when the
    walk reached the end of the window; otherwise it is a sentence naming why it
    stopped, and the caller must carry it into the report.

    It returned the bare list, and both early exits - a failed page and the
    50-page cap - printed a WARN to stderr and handed back a PARTIAL set with no
    signal. The report is a FILE, so stderr is not in it: `render_markdown` then
    printed "Window: last N days" over a walk that had covered part of it, and
    every per-skill count and percentile below was computed on the fragment.
    That is the shape `.claude/rules/scope-claims.md` names - the method
    enumerated some pages, the sentence asserted a window.
    """
    from langfuse import get_client
    client = get_client()
    api = client.api
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    out: list = []
    page = 1
    truncated: str | None = None
    while True:
        try:
            resp = api.trace.list(from_timestamp=cutoff, page=page, limit=page_size)
        except Exception as e:
            truncated = (f"the Langfuse query failed at page {page} after "
                         f"{len(out)} trace(s): {e}")
            print(f"{YELLOW}WARN {truncated}{RESET}", file=sys.stderr)
            break
        batch = list(resp.data or [])
        if not batch:
            break
        out.extend(batch)
        if len(batch) < page_size:
            break
        page += 1
        # Safety cap so a runaway query doesn't burn API budget.
        if page > 50:
            truncated = (f"stopped at the 50-page safety cap with {len(out)} "
                         f"trace(s); more traces exist in this window")
            print(f"{YELLOW}WARN {truncated}{RESET}", file=sys.stderr)
            break
    return out, truncated


def _extract_signals(trace) -> dict | None:
    """Pull downgrade signals + vendor info from a trace's metadata.

    Returns None when the trace was not produced by llm_fallback (no
    metadata, or pre-Track-B trace).
    """
    md = getattr(trace, "metadata", None) or {}
    if not isinstance(md, dict):
        return None
    signals = md.get("downgrade_signals")
    fallback_triggered = bool(md.get("fallback_triggered"))
    tags = list(getattr(trace, "tags", None) or [])
    vendor = next(
        (t.split(":", 1)[1] for t in tags if isinstance(t, str) and t.startswith("vendor:")),
        None,
    )
    if signals is None and vendor is None and not fallback_triggered:
        return None
    return {
        "vendor": vendor or "unknown",
        "fallback_triggered": fallback_triggered,
        "signals": signals,  # may be None for Gemini/Grok-served traces
    }


def aggregate(traces: list) -> dict:
    """Group by trace name; per bucket compute downgrade flag rate + sizes."""
    buckets: dict = defaultdict(lambda: {
        "total": 0,
        "by_vendor": defaultdict(int),
        "fallback_count": 0,
        "downgrade_candidates": 0,
        "flag_eligible": 0,
        "with_tool_use": 0,
        "output_tokens": [],
    })

    for tr in traces:
        info = _extract_signals(tr)
        if info is None:
            continue
        name = getattr(tr, "name", "unknown") or "unknown"
        b = buckets[name]
        b["total"] += 1
        b["by_vendor"][info["vendor"]] += 1
        if info["fallback_triggered"]:
            b["fallback_count"] += 1
        sig = info["signals"]
        if isinstance(sig, dict):
            # A trace can only be FLAGGED if it carries signals at all. Gemini
            # and Grok-served traces have `signals is None`, so they can never
            # increment the numerator -- and the rate used to divide by
            # `total`, which counts them. Any skill with fallback traffic had
            # its downgrade rate silently understated, and the column header
            # presents it as the rate for that skill.
            b["flag_eligible"] += 1
            if sig.get("downgrade_candidate"):
                b["downgrade_candidates"] += 1
            if sig.get("has_tool_use"):
                b["with_tool_use"] += 1
            tok = sig.get("output_tokens")
            if isinstance(tok, int):
                b["output_tokens"].append(tok)

    # Materialize defaultdicts so JSON serialisation works
    return {
        name: {
            "total": b["total"],
            "by_vendor": dict(b["by_vendor"]),
            "fallback_count": b["fallback_count"],
            "downgrade_candidates": b["downgrade_candidates"],
            "flag_eligible": b["flag_eligible"],
            "downgrade_pct": (
                b["downgrade_candidates"] / b["flag_eligible"] * 100
            ) if b["flag_eligible"] else 0.0,
            "with_tool_use": b["with_tool_use"],
            "median_output_tokens": (
                int(statistics.median(b["output_tokens"])) if b["output_tokens"] else None
            ),
            "p90_output_tokens": (
                int(statistics.quantiles(b["output_tokens"], n=10)[-1])
                if len(b["output_tokens"]) >= 10 else None
            ),
        }
        for name, b in buckets.items()
    }


def render_markdown(agg: dict, window_days: int, run_iso: str, total_traces: int,
                    truncated: str | None = None) -> str:
    today = _local_today_iso()
    lines: list[str] = []
    lines.append(f"# LLM-fit report - {today}")
    lines.append("")
    if truncated:
        # First, above the window sentence, because everything below it -
        # counts, percentages, medians, the P90 - was computed on the fragment.
        lines.append(f"> **INCOMPLETE FETCH.** {truncated}. Every number below "
                     f"covers only the traces that were fetched, not the whole "
                     f"window. Re-run before acting on it.")
        lines.append("")
    scope = "last {} days".format(window_days) if not truncated else (
        "part of the last {} days (see the notice above)".format(window_days))
    lines.append(f"Window: {scope}. Run: {run_iso}. "
                 f"Traces with llm_fallback metadata: {sum(b['total'] for b in agg.values())} "
                 f"(of {total_traces} fetched).")
    lines.append("")
    lines.append("**What this is.** Objective downgrade-audit per Track B of the "
                 "ARCHIVED plan 2026-05-24-llm-fit-logging-three-tracks.md, under "
                 "archive/2026/ in the `plans` records (resolve them with "  # leak-guard: ok (prose naming where the archived plan sits, never joined)
                 "`python scripts/data-path.py path plans`). No model judges "
                 "another model here - the `downgrade_candidate` flag is mechanical "
                 "from the Anthropic response shape (output_tokens < 500, no tool_use, "
                 "stop_reason == end_turn, and tier is not already Haiku).")
    lines.append("")
    lines.append("**What this is NOT.** A routing recommendation. The flags surface "
                 "patterns; acting on them is a separate later decision, not part of Phase 2.")
    lines.append("")

    if not agg:
        lines.append("_No traces with llm_fallback metadata yet. Either nothing was "
                     "wired through the fallback wrapper in this window, or Langfuse was "
                     "disabled. Re-check in a few days once normal traffic accumulates._")
        lines.append("")
        return "\n".join(lines)

    lines.append("## Per-skill summary")
    lines.append("")
    lines.append("| Skill / trace | Total | Anthropic | Fallback | Downgrade flag % (of flag-eligible) | Median out tok | P90 out tok | Tool use |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")

    sorted_buckets = sorted(agg.items(), key=lambda kv: kv[1]["total"], reverse=True)
    for name, b in sorted_buckets:
        anthropic_n = b["by_vendor"].get("anthropic", 0)
        med = b["median_output_tokens"]
        p90 = b["p90_output_tokens"]
        lines.append(
            f"| {name} | {b['total']} | {anthropic_n} | {b['fallback_count']} | "
            f"{b['downgrade_pct']:.1f}% | "
            f"{med if med is not None else '-'} | "
            f"{p90 if p90 is not None else '-'} | "
            f"{b['with_tool_use']} |"
        )
    lines.append("")

    # Highlight the top downgrade candidates
    candidates = [(n, b) for n, b in agg.items() if b["downgrade_candidates"] >= 3]
    if candidates:
        lines.append("## Top downgrade candidates (>=3 flagged in window)")
        lines.append("")
        candidates.sort(key=lambda kv: kv[1]["downgrade_pct"], reverse=True)
        for name, b in candidates:
            lines.append(
                f"- **{name}**: {b['downgrade_candidates']}/{b['flag_eligible']} flagged "
                f"({b['downgrade_pct']:.1f}%). Median {b['median_output_tokens']} output "
                f"tokens. If this skill is on Sonnet/Opus, a Haiku swap is worth "
                f"manual A/B-testing before formalising the route."
            )
        lines.append("")

    fallback_rows = [(n, b) for n, b in agg.items() if b["fallback_count"] > 0]
    if fallback_rows:
        lines.append("## Fallback events (Anthropic -> cross-vendor in this window)")
        lines.append("")
        for name, b in sorted(fallback_rows, key=lambda kv: kv[1]["fallback_count"], reverse=True):
            lines.append(
                f"- **{name}**: {b['fallback_count']} fallback(s) of {b['total']} call(s). "
                f"Vendor mix: {dict(b['by_vendor'])}."
            )
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("_Generated by `scripts/llm-fit-report.py`. Re-run any time; "
                 "its systemd-user timer runs it weekly on Sunday 03:00 local._")
    return "\n".join(lines)


def write_report(content: str) -> Path:
    target = report_dir()
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{_local_today_iso()}_llm-fit-report.md"
    path.write_text(content, encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Weekly LLM-fit downgrade-audit report.")
    parser.add_argument("--days", type=int, default=7, help="Lookback window (default: 7).")
    parser.add_argument("--json", action="store_true", help="Print JSON aggregate to stdout.")
    parser.add_argument("--no-write", action="store_true", help="Skip writing the .md file.")
    args = parser.parse_args(argv)

    load_env()
    run_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    print(f"{CYAN}Fetching Langfuse traces (last {args.days}d)...{RESET}", file=sys.stderr)
    traces, truncated = fetch_traces(args.days)
    print(f"{GRAY}  {len(traces)} traces fetched{RESET}", file=sys.stderr)

    agg = aggregate(traces)

    if args.json:
        print(json.dumps({
            "run_iso": run_iso,
            "window_days": args.days,
            "total_traces_fetched": len(traces),
            # Present and null on a complete walk, so a consumer that reads this
            # key gets an answer rather than a KeyError it may treat as "fine".
            "truncated": truncated,
            "buckets": agg,
        }, indent=2))
        return 0

    md = render_markdown(agg, args.days, run_iso, len(traces), truncated)
    if args.no_write:
        print(md)
    else:
        path = write_report(md)
        print(f"{GREEN}Report written: {path}{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
