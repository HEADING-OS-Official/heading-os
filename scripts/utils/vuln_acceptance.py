#!/usr/bin/env python3
"""The one reader of `config/accepted-vulnerabilities.json`, for both CVE gates.

Two gates audit this repository's dependencies and neither had any way to record
an accepted risk: the `pip-audit-cve` pre-commit hook over `requirements.txt`,
and `scripts/audit-deps.py` over the full locked graph in CI. Before this module
the only way past either was `--no-verify`, which `.claude/rules/security.md`
forbids outright, so a vulnerability with no published fix could not be shipped
under a recorded decision at all -- only by disarming the gate.

**One list, read by both.** This repository's dominant defect shape is a fix that
landed in one of N copies, so the acceptance list is a single file and this is
its single reader. A gate that grew its own ignore flag would be the second copy,
and the second copy is the one that stops being maintained.

**An acceptance expires.** `expires` is required and enforced: an entry past its
date is REFUSED, not ignored, and the gate goes red exactly as it did before the
entry existed. That is the difference between a dated decision and a permanent
hole, and it is why the expiry is not optional.

**Every field is required.** A missing `reason`, `accepted_by`, `accepted_on` or
`expires` raises. An acceptance with no owner and no rationale is
indistinguishable from an advisory somebody wanted to stop seeing, and the gate
cannot tell them apart by looking at the id.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
ACCEPTANCE_FILE = ROOT / "config" / "accepted-vulnerabilities.json"

REQUIRED_FIELDS = ("id", "package", "summary", "reason",
                   "accepted_by", "accepted_on", "expires")


class AcceptanceError(RuntimeError):
    """The acceptance list is unusable, so no advisory may be suppressed."""


def _parse_date(value: str, field: str, entry_id: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise AcceptanceError(
            f"{entry_id}: {field} is {value!r}, which is not an ISO date "
            "(YYYY-MM-DD)") from exc


def _today() -> date:
    """The operator's local date, not the host's naive one.

    An expiry is a decision the operator dated, so it has to fall due on the
    operator's calendar day. `date.today()` reads whatever zone the machine
    happens to sit in, which on a laptop that travels is not the same question
    (`.claude/rules/voice.md`, and DTZ011 in the lint ratchet).
    """
    from scripts.utils.workspace import get_default_tz

    return datetime.now(tz=get_default_tz()).date()


def load_acceptances(path: Path | None = None, *, today: date | None = None) -> list[dict]:
    """Every live acceptance, validated. Raises rather than returning a subset.

    An absent file is an empty list, which is the correct reading: a repository
    that has never accepted anything suppresses nothing. A file that EXISTS and
    cannot be read is an error, because at that point something was written down
    and the gate cannot tell what.
    """
    path = path or ACCEPTANCE_FILE
    today = today or _today()

    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptanceError(f"{path} exists but could not be read: {exc}") from exc

    entries = payload.get("accepted", [])
    if not isinstance(entries, list):
        raise AcceptanceError(f"{path}: 'accepted' must be a list")

    live, expired = [], []
    for entry in entries:
        missing = [f for f in REQUIRED_FIELDS if not entry.get(f)]
        if missing:
            raise AcceptanceError(
                f"{entry.get('id', '<no id>')}: missing required field(s) "
                f"{', '.join(missing)}. An acceptance with no reason and no "
                "owner cannot be told apart from an advisory someone wanted to "
                "hide.")
        _parse_date(entry["accepted_on"], "accepted_on", entry["id"])
        expiry = _parse_date(entry["expires"], "expires", entry["id"])
        (expired if expiry < today else live).append(entry)

    if expired:
        raise AcceptanceError(
            "expired acceptance(s): "
            + "; ".join(f"{e['id']} expired {e['expires']}" for e in expired)
            + ". Re-take the decision or fix the dependency; an expiry that "
            "keeps working is a permanent hole wearing a date.")
    return live


def ignore_flags(path: Path | None = None, *, today: date | None = None) -> list[str]:
    """The `--ignore-vuln <id>` arguments to hand pip-audit, and nothing else."""
    flags: list[str] = []
    for entry in load_acceptances(path, today=today):
        flags += ["--ignore-vuln", entry["id"]]
    return flags


def describe(path: Path | None = None, *, today: date | None = None) -> str:
    """One line per live acceptance, as text. Gates call `announce()` instead.

    A suppressed advisory that leaves no trace in the log is a gate that reports
    clean over a scope it narrowed, which `.claude/rules/scope-claims.md` names
    as the same defect as over-claiming coverage. This returns the text and
    writes nothing, so a caller that wants it printed chooses the stream; both
    gates chose stdout, which is the wrong one (see `announce`).
    """
    entries = load_acceptances(path, today=today)
    if not entries:
        return ""
    lines = [f"CVE gate: {len(entries)} accepted advisory(ies) suppressed, "
             f"per {ACCEPTANCE_FILE.relative_to(ROOT)}:"]
    lines += [f"  {e['id']}  {e['package']} {e.get('version', '')}  "
              f"accepted by {e['accepted_by']} on {e['accepted_on']}, "
              f"expires {e['expires']}" for e in entries]
    return "\n".join(lines)


def announce(path: Path | None = None, *, today: date | None = None) -> str:
    """Write the notice to STDERR, for every gate, and return what was written.

    The notice is a diagnostic beside the audit's own output, not part of it.
    `scripts/audit-deps.py --json` documents stdout as pip-audit's JSON stream
    and nothing else; the pre-commit hook's stdout is pip-audit's report. Each
    of them called `describe()` and `print()`ed the result itself, and each put
    it on stdout. MEASURED 2026-09-13: `audit-deps.py --json` stdout began with
    "CVE gate: 1 accepted advisory(ies) suppressed, per
    config/accepted-vulnerabilities.json:" and one advisory line before the
    JSON document, so `--json | json.tool` died on the first byte; after this
    function replaced both `print(note)` calls, stdout is empty and the same
    two lines are on stderr. The stream is chosen HERE, once, so a third gate
    cannot pick the wrong one by calling `describe()` and printing.

    An empty notice writes nothing: a repository that accepted nothing has
    nothing to trace. `sys.stderr` is looked up at call time, not bound as a
    default, so a capture installed after import still sees the write.
    """
    note = describe(path, today=today)
    if note:
        print(note, file=sys.stderr)
    return note


if __name__ == "__main__":
    # stdout on purpose: here the notice IS the program's output, not a
    # diagnostic beside somebody else's. `announce()` is for the gates.
    print(describe() or "CVE gate: no accepted advisories.")
