"""The org-roster spreadsheet, read once for every caller that needs it.

Every caller reads the workbook through this module, so a fix lands once;
`scripts/crm-health.py --roster` is the terminal path to the list.

MEASURED 2026-09-20 on the operator's overlay, and it is why the terminal path
was asked for: most of the people in the workbook carried no CRM card with a
team relationship type, so they were reachable from the record reader and from
nowhere else.

The reader degrades to `[]` on every failure (file absent, `openpyxl` absent,
workbook unreadable, no header row), because a caller that renders a page needs
an empty list rather than an exception. A caller that wants to tell "empty" from
"could not read" asks `roster_path(...).exists()` first; this module deliberately
does not invent a third return value for it.
"""
from __future__ import annotations

import logging
from pathlib import Path

from scripts.utils.data_path import data_path

logger = logging.getLogger(__name__)

#: The workbook's file name inside the registry's `tribe-operations` when that
#: directory does not hold exactly one workbook. With exactly one, that one is
#: the roster, whatever the company named it. It carried the pre-move directory
#: in front of it until 2026-09-25, as the question the migration seam was
#: asked; the seam is gone and the directory is the registry's answer.
TRIBE_ROSTER_XLSX = "team-roster.xlsx"

#: The sheet the workbook is expected to carry: the first whose name ends in
#: this. Missing, the first sheet is used.
ROSTER_SHEET_SUFFIX = "Roster"


def roster_path(data_root: Path) -> Path:
    """The org-roster spreadsheet, before and after the HEADING OS 2 move.

    Kind: `build/runbook`. MEASURED 2026-09-18, the morning after the move: the
    legacy directory still existed and the workbook was not in it, so a bare
    join answered an absent path in silence and the team page degraded to
    CRM-only with nothing saying why. The seam resolves it. Resolution cost,
    measured the same day: 1.15 s on the first call, because `build/runbook` is
    a moving kind and the seam walks its 136 records rather than reading a
    flipped table. Stated rather than hidden: it is paid once per call and it
    buys a list that is not silently empty.

    `data_root` is taken explicitly rather than fetched, so a test can sandbox
    it and the suite never reaches the operator's real overlay.

    OFF THE SEAM ON 2026-09-22, and through the DIRECTORY's name rather than the
    workbook's, which is the whole of the care this line needs. HOS-5 phase 6
    kept the seam here because this function must ANSWER rather than refuse: the
    module docstring above tells a caller to ask `roster_path(...).exists()` to
    tell "empty" from "could not read", and `scripts/crm-health.py` does exactly
    that on the result, so a raise would land inside an `.exists()` in a file
    outside this one. The name it weighed was `tribe-workbook`, and that name is
    DELIBERATELY absent from `scripts/data-names.example.yaml`: a particular
    workbook file is not structure, and `template_names()` says so in its own
    docstring. MEASURED 2026-09-22 on a data root with no registry of its own,
    `data_path("tribe-workbook")` still raises `UnknownDataPath`.

    `tribe-operations` is the directory that holds it, is structure, and the
    engine ships it. MEASURED the same day: on the operator's overlay the
    workbook in `data_path("tribe-operations")` is the file the
    `tribe-workbook` name answers, under `build/runbook`, which is
    the path the seam answered and the file that exists. So the
    degradation the paragraph above depends on is kept, and the 1.15 s the seam
    spent walking 136 records of a moving kind is not.

    The workbook is the one `*.xlsx` in that directory; with none or several,
    `TRIBE_ROSTER_XLSX` there, which may be absent, and the caller's
    `.exists()` then says so.

    NOT reconciled, and named rather than left to be found: a team bot that
    asks `tribe-workbook` for what is presumably this same file raises, on a
    tree with no registry of its own, where this one answers.
    """
    base = data_path("tribe-operations", data_root=data_root)
    books = sorted(base.glob("*.xlsx")) if base.is_dir() else []
    return books[0] if len(books) == 1 else base / TRIBE_ROSTER_XLSX


def load_roster(data_root: Path) -> list[dict]:
    """Read the org roster. Returns a list of dicts, or `[]` on any failure.

    Each row: `{name, email, title, department, reports_to, telegram}`. The
    email is lowercased, because it is what the CRM half joins on.
    """
    path = roster_path(data_root)
    if not path.exists():
        return []
    try:
        import openpyxl
    except ImportError:
        return []
    try:
        wb = openpyxl.load_workbook(path, data_only=True)
    except Exception:  # noqa: BLE001 - any load failure degrades to an empty list
        logger.warning("tribe roster: workbook at %s could not be opened",
                       path.name, exc_info=True)
        return []
    try:
        named = [s for s in wb.sheetnames if s.endswith(ROSTER_SHEET_SUFFIX)]
        ws = wb[named[0] if named else wb.sheetnames[0]]
        rows = [tuple(r) for r in ws.iter_rows(values_only=True)]
    except Exception:  # noqa: BLE001 - any read failure degrades to an empty list
        logger.warning("tribe roster: workbook at %s could not be read",
                       path.name, exc_info=True)
        return []
    finally:
        try:
            wb.close()
        except Exception as exc:  # noqa: BLE001 - close is best-effort
            logger.debug("tribe roster: workbook close failed: %s", exc)

    # Locate the header row, which is the one carrying both 'name' and 'email'.
    # The workbook opens with a title row above it, so the header is never row 0.
    header = None
    header_idx = -1
    for i, r in enumerate(rows):
        cells = [str(c).strip().lower() if c is not None else "" for c in r]
        if "name" in cells and "email" in cells:
            header, header_idx = cells, i
            break
    if header is None:
        return []

    def col(*needles):
        for j, c in enumerate(header):
            if c in needles:
                return j
        for j, c in enumerate(header):
            if any(n in c for n in needles):
                return j
        return None

    ci = {
        "name": col("name"),
        "email": col("email"),
        "title": col("title"),
        "department": col("function / department", "department"),
        "reports_to": col("reports to"),
        "telegram": col("telegram"),
    }
    if ci["name"] is None:
        return []

    def cell(r, idx):
        if idx is None or idx >= len(r) or r[idx] is None:
            return ""
        return str(r[idx]).strip()

    roster = []
    for r in rows[header_idx + 1:]:
        if not r:
            continue
        name = cell(r, ci["name"])
        if not name:
            continue
        roster.append({
            "name": name,
            "email": cell(r, ci["email"]).lower(),
            "title": cell(r, ci["title"]),
            "department": cell(r, ci["department"]),
            "reports_to": cell(r, ci["reports_to"]),
            "telegram": cell(r, ci["telegram"]),
        })
    return roster
