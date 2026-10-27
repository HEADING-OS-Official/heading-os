"""Structured run record for `/scrutinize` - the writer and its validator.

Why this exists, measured rather than assumed. Across 75 saved scrutiny reports
the mandated `Refutation:` header appears in 8 files and the mandated
`## Judge layer` heading in 12. Both are prose mandates written by a model that
can omit them silently, and 50 reports leave no machine-readable trace of whether
the refutation phase ran at all. This module moves authorship of that trace to
code.

**What it does NOT claim.** It cannot make omission impossible - the Claude-side
verdict is still supplied by the running session, because that judge IS the
running session. What it makes is omission VISIBLE: `validate()` fails when a
run has no `pass_start` row, when a report's `Refutation:` header claims a pass
it has too few verdict rows for, and when a declared skip carries no matching
`degraded` row. Any claim stronger than "visible" is false and must not appear
here or in the skill's documentation.

The non-circular signal is deliberate. A report ASSEMBLED from these rows and
then validated against them would test generation, not compliance. So the
validator reads the one line a human wrote the mandate for - the `Refutation:`
header in the approval block - and reconciles it against the rows.

Consumed by `scripts/scrutinize-record.py` (thin CLI), `scripts/scrutinize-dispatch.py`,
and `scripts/scrutinize-flag-fp.py`.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.utils.data_path import data_path, data_read  # noqa: E402
from scripts.utils.jsonl_lines import jsonl_lines  # noqa: E402

# ============================================================
# Vocabulary
# ============================================================
KINDS = frozenset({
    "pass_start", "verdict", "reproduction", "role", "currency", "fp_flag", "degraded",
})

VERDICTS = frozenset({
    "REFUTED", "REFUTE_PARTIAL", "REFUTATION_FAILED",
    "CORRECT", "CORRECT_DOWNGRADE", "INCORRECT", "AMBIGUOUS",
    "REPRODUCED", "FALSIFIED",
})

ROLES = frozenset({"ops", "scheduler", "boundary"})

# The judge roster, ORDERED, because `scripts/scrutinize-dispatch.py` derives the
# per-run role assignment from this sequence and an unordered set would make that
# assignment depend on the interpreter's hash seed. It lives here rather than in
# the dispatcher because the dispatcher is a kebab-case CLI that no module can
# import, and this file is what REFUSES a row naming a family outside the roster:
# the writer's vocabulary and the dispatcher's choices have to be the same list,
# or a family the dispatcher will assign is a family `append_row` raises on.
# ChatGPT (`gpt-6-astra` via `get_model("chatgpt_reasoning")`) joined 2026-09-11.
JUDGE_FAMILIES = ("claude", "kimi", "chatgpt")
FAMILIES = frozenset(JUDGE_FAMILIES)
CURRENCY_RESULTS = frozenset({"ok", "mismatch", "inconclusive"})

# The header the approval block mandates. Its presence is the compliance signal.
_REFUTATION_RE = re.compile(r"^Refutation:\s*(?P<value>.+)$", re.MULTILINE)
# "Findings: 0 BLOCKER, 2 HIGH, 4 MEDIUM, ..." - the judged count is what 2.5
# covers, which is BLOCKER + HIGH + MEDIUM per refutation-protocol.md.
_JUDGED_RE = re.compile(r"(?P<n>\d+)\s+(?P<sev>BLOCKER|HIGH|MEDIUM)\b")
_SKIP_RE = re.compile(r"\bskipped\b", re.IGNORECASE)


def record_path() -> Path:
    """The JSONL record, under the DATA overlay. Monkeypatched in tests.

    The registry name `scrutiny-sheets` is where the reports also live, so the
    reader in `scrutiny_report_dirs()` and this writer cannot part again (they
    did, MEASURED 2026-09-18).
    """
    return data_path("scrutiny-sheets") / "runs.jsonl"


def scrutiny_report_dirs() -> list[Path]:
    """Every EXISTING home of the two report names, deduplicated, in order.

    The mover filed most /scrutinize reports as `("trace", "scratch")` and one as
    `("trace", "report")`, so a reader asks both names: `scrutiny-sheets` and
    `scrutiny-reports`. MEASURED 2026-09-25 on the operator's overlay: 83 and
    1, exactly the 84 the previous compat lookup found. Each name is spelled at
    its own call so a gate reading this file can say which data it names.
    `data_read` answers every place a reader must look, which for a record kind
    is many candidate directories of which few exist, so only the ones on disk
    are kept.
    """
    seen: dict[Path, None] = {}
    for home in data_read("scrutiny-sheets") + data_read("scrutiny-reports"):
        if home.is_dir():
            seen[home] = None
    return list(seen)


def scrutiny_reports() -> list[Path]:
    """Every saved scrutiny report on disk now, under both report names.

    THE shared root for this lookup, here since 2026-09-18 rather than in
    either CLI: MEASURED that day, two copies of one lookup had parted, and one
    of them exited 3 with "no scrutiny reports found in range" over 83 reports
    on disk. Recursive, because a report filed in a dated subdirectory is still
    a saved report.

    It took a `legacy` directory until HOS-5 section 6 (2026-09-25).
    """
    found: dict[Path, None] = {}
    for home in scrutiny_report_dirs():
        for path in home.rglob("*.md"):
            if path.is_file():
                found[path] = None
    return sorted(found)


def scrutiny_dirs() -> list[Path]:
    """Where the reports were actually found, for a not-found message.

    Falls back to the first report name when nothing is on disk, so the
    message names a directory rather than nothing at all.
    """
    dirs = sorted({p.parent for p in scrutiny_reports()})
    return dirs or [data_path("scrutiny-sheets")]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ============================================================
# Append
# ============================================================
def _check(row: dict) -> None:
    """Refuse a row the record must never carry. Raises ValueError."""
    kind = row.get("kind")
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; expected one of {sorted(KINDS)}")

    verdict = row.get("verdict")
    if verdict is not None and verdict not in VERDICTS:
        raise ValueError(f"unknown verdict {verdict!r}")

    family = row.get("judge_family")
    if family is not None and family not in FAMILIES:
        raise ValueError(f"unknown judge_family {family!r}; the roster is {sorted(FAMILIES)}")

    role = row.get("role")
    if role is not None and role not in ROLES:
        raise ValueError(f"unknown role {role!r}")

    # isinstance BEFORE .get, on both of the two dict-shaped columns. Every
    # other refusal in this function is a ValueError and the docstring promises
    # only that, but a caller passing a bare string got an AttributeError out of
    # `.get` instead. Measured 2026-08-30: append_row(kind="currency",
    # currency="ok") and append_row(kind="verdict", verdict="REPRODUCED",
    # reproduction="nope") both raised AttributeError, so a caller catching
    # ValueError to report a rejected row crashed rather than refusing it.
    currency = row.get("currency")
    if currency is not None:
        if not isinstance(currency, dict):
            raise ValueError(f"currency must be a mapping, got {type(currency).__name__}")
        if currency.get("result") not in CURRENCY_RESULTS:
            raise ValueError(f"unknown currency result {currency.get('result')!r}")

    reproduction = row.get("reproduction")
    if reproduction is not None and not isinstance(reproduction, dict):
        raise ValueError(
            f"reproduction must be a mapping, got {type(reproduction).__name__}")

    # The whole point of REPRODUCED / FALSIFIED: the exit codes are observed by
    # the harness, never narrated. A row that cannot show them does not land.
    if verdict in ("REPRODUCED", "FALSIFIED"):
        repro = reproduction or {}
        before = repro.get("exit_before")
        if not isinstance(before, int) or before == 0:
            raise ValueError(
                f"{verdict} requires an observed non-zero exit_before, got {before!r}")
        if verdict == "FALSIFIED":
            after = repro.get("exit_after")
            if after != 0:
                raise ValueError(
                    f"FALSIFIED requires an observed zero exit_after, got {after!r}")


def append_row(
    *,
    run_id: str,
    kind: str,
    target: str,
    finding_id: str | None = None,
    pass_: str | None = None,
    judge_family: str | None = None,
    verdict: str | None = None,
    confidence_before: int | None = None,
    confidence_after: int | None = None,
    reproduction: dict | None = None,
    role: str | None = None,
    currency: dict | None = None,
    degraded: str | None = None,
    writer: str = "dispatch",
    payload: dict | None = None,
) -> dict:
    """Validate and append one row. Returns the row written."""
    row: dict[str, Any] = {
        "run_id": run_id,
        "ts": _now_iso(),
        "target": target,
        "kind": kind,
        "finding_id": finding_id,
        "pass": pass_,
        "judge_family": judge_family,
        "verdict": verdict,
        "confidence_before": confidence_before,
        "confidence_after": confidence_after,
        "reproduction": reproduction,
        "role": role,
        "currency": currency,
        "degraded": degraded,
        "writer": writer,
        # Free-form, kind-specific detail. `fp_flag` carries the operator's own
        # words here: the statement he is overruling and the reason he gave. The
        # fixed columns above have nowhere to put prose, and dropping it made the
        # CLI print "Note attached" over a row that held only an ID.
        "payload": payload,
    }
    _check(row)

    path = record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    # O_APPEND on a REGULAR FILE guarantees that the seek-to-end and the write
    # are one operation, so two concurrent judge dispatches cannot overwrite
    # each other's bytes. It does NOT guarantee a single `write()` lands whole.
    # The PIPE_BUF atomicity this comment used to cite is defined for pipes and
    # FIFOs and says nothing about this file.
    #
    # So the return of `os.write` is checked rather than assumed. It was
    # discarded, and a short write silently truncated the row: measured
    # 2026-08-30 with `os.write` stubbed to take 20 bytes, `append_row` returned
    # the row as written, the file held `{"run_id": "r", "ts"`, and `iter_rows`
    # dropped that line on JSONDecodeError and answered []. A verdict row
    # vanished with nothing raising anywhere, in the one module whose purpose is
    # not trusting unverified claims of completeness.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        written = 0
        while written < len(data):
            sent = os.write(fd, data[written:])
            if sent <= 0:
                raise OSError(
                    f"short write to {path}: {written} of {len(data)} bytes; "
                    "the record now holds a truncated row")
            written += sent
    finally:
        os.close(fd)
    return row


def iter_rows() -> list[dict]:
    """Every well-formed row in the record, in write order. No file means none."""
    path = record_path()
    if not path.exists():
        return []
    # A row it cannot use is skipped and counted; the rest still comes back.
    # One undecodable byte used to raise out of every cross-run tally
    # (MEASURED 2026-09-01), and several writers append here, so a torn write
    # is the ordinary corruption. `jsonl_lines` decodes per line.
    out = []
    dropped = 0
    try:
        lines = list(jsonl_lines(path))
    except OSError as exc:
        print(f"[scrutinize-record] {path} unreadable ({exc}); reporting no rows",
              file=sys.stderr)
        return []
    for line in lines:
        if line is None:
            dropped += 1
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            dropped += 1
            continue
        out.append(row)
    if dropped:
        # Said, not swallowed. Every cross-run tally in the system counts what
        # this returns, so a silently shorter list is a wrong number nobody can
        # see.
        print(f"[scrutinize-record] {path}: skipped {dropped} unreadable line(s)",
              file=sys.stderr)
    return out


def rows_for(run_id: str) -> list[dict]:
    """Every row belonging to one run, in write order. Missing file means none."""
    return [r for r in iter_rows() if r.get("run_id") == run_id]


def rows_of_kind(kind: str) -> list[dict]:
    """Every row of one kind across all runs - what a cross-run tally reads."""
    return [r for r in iter_rows() if r.get("kind") == kind]


def flagged_pairs() -> set[tuple[str, str]]:
    """The DISTINCT (run_id, finding_id) pairs the CEO has flagged as false
    positives, read from the record's `fp_flag` rows. A finding is flagged or
    it is not; flagging it twice does not make it twice as false."""
    out: set[tuple[str, str]] = set()
    for row in rows_of_kind("fp_flag"):
        sid, fid = row.get("run_id"), row.get("finding_id")
        if sid and fid:
            out.add((str(sid), str(fid)))
    return out


def last_reproduction(run_id: str, finding_id: str) -> dict | None:
    """The most recent reproduction row for a finding, or None.

    `promote()` joins against this: a FALSIFIED verdict is the marriage of a
    stored pre-fix exit and a freshly observed post-fix one.
    """
    for row in reversed(rows_for(run_id)):
        if row.get("kind") == "reproduction" and row.get("finding_id") == finding_id:
            return row
    return None


# ============================================================
# Validate - the part that has to fail on silence
# ============================================================
def _judged_count(report_text: str) -> int | None:
    """BLOCKER + HIGH + MEDIUM from the report's Findings line - what 2.5 covers.

    None means the line is ABSENT, which is not the same as zero and used to be
    reported as zero. With no `Findings:` line the sum was 0, `len(verdict_rows)
    < 0` is false for every possible row count, and `validate()` returned clean
    over a report claiming a complete refutation pass backed by no verdict rows
    at all. Reproduced 2026-08-26 with the counts written under a `Summary:`
    label instead. This module's own section header calls itself "the part that
    has to fail on silence", and a missing count is silence.
    """
    for candidate in report_text.splitlines():
        if candidate.startswith("Findings:"):
            return sum(int(m.group("n")) for m in _JUDGED_RE.finditer(candidate))
    return None


def validate(*, run_id: str, report_path: Path) -> list[str]:
    """Reconcile a saved report against the run's rows. Empty list means clean.

    Three failure modes, and the first is the one this record exists for: a pass
    that never called the dispatcher produces no rows AND no verdict prose, which
    a mismatch-only check cannot distinguish from a legitimate skip.
    """
    defects: list[str] = []
    rows = rows_for(run_id)

    if not any(r.get("kind") == "pass_start" for r in rows):
        defects.append(
            f"run {run_id} has no pass_start row: either the pass never called the "
            f"dispatcher, or it called it without opening the run")

    report_path = Path(report_path)
    if not report_path.exists():
        defects.append(f"report not found: {report_path}")
        return defects

    text = report_path.read_text(encoding="utf-8")
    match = _REFUTATION_RE.search(text)
    if not match:
        defects.append(
            "report carries no 'Refutation:' header; approval-block.md mandates it, "
            "and it is the only signal the rows can be reconciled against")
        return defects

    claim = match.group("value").strip()
    verdict_rows = [r for r in rows if r.get("kind") == "verdict"]

    if _SKIP_RE.search(claim):
        if not any(r.get("kind") == "degraded" for r in rows):
            defects.append(
                f"report declares a skipped refutation ({claim!r}) but the run has no "
                f"degraded row naming the cause")
    else:
        judged = _judged_count(text)
        if judged is None:
            defects.append(
                f"'Refutation: {claim}' claims a completed pass but the report "
                f"carries no 'Findings:' line, so the judged count cannot be "
                f"established and the {len(verdict_rows)} verdict row(s) cannot "
                f"be reconciled against anything")
        elif len(verdict_rows) < judged:
            defects.append(
                f"'Refutation: {claim}' claims a pass over {judged} judged finding(s) "
                f"but the run holds {len(verdict_rows)} verdict row(s)")

    return defects
