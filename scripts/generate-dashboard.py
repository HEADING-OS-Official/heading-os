#!/usr/bin/env python3
"""
CEO Morning Dashboard Generator

Aggregates CRM health, pipeline, calendar, email, strategy, metrics, and
data-freshness into a single-page HTML dashboard. Self-contained (inline CSS,
base64 logo, no external dependencies beyond Google Fonts).

Usage:
    python scripts/generate-dashboard.py                     # HTML only
    python scripts/generate-dashboard.py --pdf               # HTML + PDF
    python scripts/generate-dashboard.py --output-dir DIR    # custom output dir

Coverage: none live. Both of the runs that held this dashboard, one on the
morning calendar's timezone and one on the pipeline table, were parked in the
2026-09-21 test-suite split. A regression now shows up in the dashboard itself.
"""

import argparse
import base64
import html
import json
import math
import re
import subprocess
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.utils.operator_identity import company_brand  # noqa: E402

# The company in the dashboard's cover and footers, from operator.yaml; a clone
# with none configured prints no company.
_CO_NAME, _CO_SHORT, _CO_SITE = company_brand()
_CO_ALT = _CO_NAME or "Logo"
_CO_CREDIT = " &middot; ".join(p for p in (f"&copy; 2025-2026 / {_CO_NAME}" if _CO_NAME else "", _CO_SITE) if p)
from scripts.utils.brand_assets import brand_asset_path
from scripts.utils.html_templates import render_template
from scripts.utils.image import load_logo_base64
from scripts.utils.crm import (
    crm_config_path as _crm_config_path,
    parse_config as _crm_parse_config,
    people_path as _crm_people_path,
    scan_contacts as _crm_scan_contacts,
)
from scripts.utils.data_homes import claimed_children
from scripts.utils.data_path import DataPathError, data_path
from scripts.utils.venv_guard import venv_python
from scripts.utils.workspace import get_data_config_dir, get_default_tz, get_knowledge_dir, make_run_clock
from scripts.utils.markdown import frontmatter_date
from scripts.utils.markdown import parse_frontmatter as _parse_fm
from scripts.utils.markdown import parse_md_table
from scripts.utils.odin_cadence import read_cadence_json

# ============================================================
# Paths
# ============================================================
SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE = SCRIPT_DIR.parent

HTML_TO_PDF_SCRIPT = SCRIPT_DIR / "html-to-pdf.py"
# R10 capture-payoff: the ceo-only cadence script.
ODIN_CADENCE_SCRIPT = SCRIPT_DIR / "odin-cadence.py"


# ============================================================
# Data-root paths - functions, never constants
# ============================================================
def _get_crm_config_path() -> Path:
    """The cadence table. Resolved once in `scripts/utils/crm.crm_config_path()`;
    kept under this name so a fixture can redirect it."""
    return _crm_config_path()


def get_people_file() -> Path:
    """`people`, resolved in `scripts/utils/crm.people_path()`; kept bound here so
    a fixture can point `collect_freshness` elsewhere."""
    return _crm_people_path()


def pipeline_file() -> Path:
    """Resolved at call time, never at import.

    Every path below reaches `get_knowledge_dir()` or `data_path()`, each of
    which reads HEADING_OS_DATA per call, so a repointed root is followed. A
    tree whose registry lacks a name raises `UnknownDataPath`.
    """
    return data_path("pipeline")


def strategy_file() -> Path:
    """The `strategy` record, one file.

    `data_read('strategy')` is deliberately NOT what this calls: it answers with
    three addresses, two of which are not this file, and the panel wants one.
    """
    return data_path("strategy")


def metrics_file() -> Path:
    return data_path("current-data")


def calendar_file() -> Path:
    # Written by scripts/sync-exchange.py under the registry name
    # `exchange-calendar-cache`. A reader asks the same name without creating
    # anything: an absent dump is "NOT SYNCED", and a read that left an empty
    # store behind would be a write from a report.
    return data_path("exchange-calendar-cache") / "upcoming.md"


def email_file() -> Path:
    return data_path("exchange-email-cache") / "inbox-latest.md"


def hiring_file() -> Path:
    return data_path("hiring-pipeline")


def dashboard_dir() -> Path:
    """Where the rendered morning dashboards go: the `dashboard` registry name.

    The one WRITING path in this module; the rest read. The registry names the
    directory, not the dated leaf, which has no records yet by definition.
    """
    return data_path('dashboard')


# The four readers below each read one registry name; an unnamed one raises
# `UnknownDataPath`, so a vacated address can never read as "none".

def assistant_inbox_tasks_file() -> Path:
    return data_path('assistant-inbox-tasks')


def assistant_inbox_state_file() -> Path:
    return data_path('assistant-inbox-state')


def newsletters_dir() -> Path:
    return data_path('newsletters')


def linkedin_dir() -> Path:
    return data_path('linkedin-drafts')


# No separate drafts reader: `linkedin-drafts` is what `linkedin_dir()` reads,
# so counting it again would count those files twice.


def linkedin_archive_dir() -> Path:
    """Where a PUBLISHED post ends up. `scripts/linkedin-archive.py` `git mv`s
    the staged .md out of `linkedin_dir()` into here, under
    {posts|articles|comments}/{slug}/, so a post counted while it sat
    unpublished stopped being counted the moment it went live.

    A module-level function so the cadence panel can be pointed at a fixture
    rather than the live archive.

    `scripts/linkedin-archive.py::archive_home` is the writer this reads
    behind, and since HOS-5 phase 6 it asks for the SAME registry name, so the
    two cannot end up on different answers the way two hand-joined literals
    could.
    """
    return data_path('linkedin-archive')


def knowledge_dir() -> Path:
    return get_knowledge_dir()


def odin_brain_dir() -> Path:
    return data_path('odin-brain')


# Canonical brand assets (per the corporate style guide, the registry name
# `corporate-style-guide` in the data repository). Each of the three asks the
# private manifest for its filename: a datastore filename is itself private
# (scripts/utils/brand_assets.py).
def logo_white_path() -> Path:
    return brand_asset_path("logo_on_dark")


def gt_light_font() -> Path:
    return brand_asset_path("font_gt_l_light")


def gt_medium_font() -> Path:
    return brand_asset_path("font_gt_l_medium")


def load_font_b64(path):
    """Read a WOFF2 font file and return a base64-encoded data URI string.
    Returns empty string if missing - caller falls back to system fonts."""
    if not path.exists():
        return ""
    return base64.b64encode(path.read_bytes()).decode("ascii")

# This run's single wall-clock reading, taken at first use rather than at
# import, and memoised: the date and time are read in twenty-two places below, from the output directory name to every age comparison, and one reading keeps a run that
# straddles midnight consistent. Why both: `make_run_clock` in
# scripts/utils/workspace.py.
_clock = make_run_clock()


def _today() -> date:
    """This run's local date. See `_clock`."""
    return _clock().date()


def _now() -> datetime:
    """This run's local timestamp. See `_clock`."""
    return _clock()


def _zone_suffix():
    """" (ZONE)" for the page furniture, or "" when the zone has no name.

    The two headers below printed the literal words "the configured timezone"
    on a page marked "Internal - CEO Eyes Only". `_now()` is built with
    `get_default_tz()`, so the abbreviation was available the whole time.
    """
    zone = _now().tzname()
    return f" ({esc(zone)})" if zone else ""


# ============================================================
# Utilities
# ============================================================
def esc(text):
    if not text:
        return ""
    return html.escape(str(text))


def read_file(path):
    """The text of a dashboard source, or "" when this cannot read it.

    Every panel already distinguishes "the source was not read" from "the source
    was empty": the collectors leave `source_read` False on a falsy return and
    `_sync_label` renders that as NOT SYNCED. A file this cannot decode belongs
    in the first of those two states, and until 2026-09-01 it reached neither -
    `read_text(encoding="utf-8")` raised `UnicodeDecodeError` (a ValueError, so
    no relation to the OSError handlers elsewhere on this page) straight out of
    `collect_calendar`, and the whole dashboard died over one byte in one source.

    Announced rather than swallowed, because "" is also what an empty file
    gives: without this line the panel would report NOT SYNCED with the reason
    recorded nowhere.
    """
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"[generate-dashboard] could not read {path}: {exc}",
              file=sys.stderr)
        return ""


# ============================================================
# Data Collectors
# ============================================================
HEALTH_BUCKETS = ("red", "yellow", "green", "gray")


def health_bucket(contact):
    """The card a contact belongs on. Never raises, never silently vanishes.

    `result[c["health"]]` was a bare lookup, so one contact whose health
    scanned as "RED" or "unknown" raised KeyError, which the broad
    `except Exception` in collect_crm_health caught -- leaving the buckets
    HALF filled and commitments_due EMPTY. The Urgent Items panel then read
    "All Clear" while overdue commitments existed.
    """
    health = str(contact.get("health") or "gray").strip().lower()
    if health not in HEALTH_BUCKETS:
        print(f"  Warning: contact {contact.get('name', '?')!r} has health "
              f"{contact.get('health')!r}; counted as gray", file=sys.stderr)
        return "gray"
    return health



def collect_crm_health():
    """Scan CRM contacts via in-process import (no subprocess overhead).

    Replicates the JSON envelope crm-health.py --json produced, then bins
    contacts into red/yellow/green/gray and surfaces commitments due in the
    next 7 days. Behaviour-preserving refactor: previously this shelled out
    to crm-health.py and re-parsed its JSON output.
    """
    result = {"contacts": [], "red": [], "yellow": [], "green": [], "gray": [],
              "commitments_due": [], "total": 0, "failed": ""}
    try:
        config = _crm_parse_config(_get_crm_config_path())
        raw_contacts, _tribe_warnings, _dangling_refs, _stages, _aliases = _crm_scan_contacts(config, today=_today())

        # Normalise to the previous JSON-derived shape: due dates as ISO strings.
        #
        # `.get`, and one try per CONTACT. Every field here was a bare index, so
        # a single contact dict missing a single key raised before ANY bucket was
        # filled; the broad `except` below then returned the empty skeleton and
        # the Urgent Items panel showed "All Clear". `health_bucket` was hardened
        # against exactly that failure shape and the normaliser above it was not,
        # so the same outcome stayed reachable through a different trigger. A bad
        # contact is now dropped alone, and named.
        contacts = []
        for c in raw_contacts:
            try:
                commits = []
                for cm in c.get("commitments", []):
                    due_iso = cm["due"].strftime("%Y-%m-%d") if cm.get("due") else None
                    commits.append({"text": cm["text"], "due": due_iso})
                contacts.append({
                    "name": c.get("name", "(unnamed)"),
                    "company": c.get("company", ""),
                    "type": c.get("type", ""),
                    "last_touch": c.get("last_touch"),
                    "cadence": c.get("cadence"),
                    "health": c.get("health"),
                    "days_since": c.get("days_since"),
                    "commitments": commits,
                    "file": c.get("file", ""),
                })
            except (KeyError, AttributeError, TypeError, ValueError) as e:
                # The name is read behind an isinstance check, not with a bare
                # `c.get(...)`. That call is the SAME operation that raises for
                # a record which is not a mapping at all, so the handler raised
                # a second AttributeError out of itself, the broad `except`
                # below caught THAT, and one non-mapping record set
                # `result["failed"]` and returned the empty skeleton. The
                # comment at the top of this loop promises the opposite: "A bad
                # contact is now dropped alone, and named." A handler must not
                # be able to fail in the way it exists to survive.
                name = c.get("name", "?") if isinstance(c, dict) else "?"
                print(f"[generate-dashboard] skipping malformed contact "
                      f"{name!r}: {e}", file=sys.stderr)

        result["contacts"] = contacts
        result["total"] = len(contacts)
        for c in contacts:
            result[health_bucket(c)].append(c)
            for commit in c.get("commitments", []):
                due = commit.get("due")
                if due:
                    try:
                        due_date = date.fromisoformat(due)
                        if due_date <= _today() + timedelta(days=7):
                            result["commitments_due"].append({
                                "name": c["name"],
                                "company": c.get("company", ""),
                                "text": commit["text"],
                                "due": due,
                                "overdue": due_date < _today(),
                            })
                    except ValueError:
                        pass
    except Exception as e:
        print(f"Warning: CRM health collection failed: {e}", file=sys.stderr)
        # Recorded, not just printed. stderr scrolls past; the PAGE is what the
        # CEO reads, and until this flag existed the page could not tell a
        # failed scan from a quiet morning. `build_urgent` reads it.
        result["failed"] = f"{type(e).__name__}: {e}"
    return result


def collect_pipeline():
    """Parse pipeline.md for deals, investors, partnerships, won."""
    # Resolved once.
    pipeline = pipeline_file()
    content = read_file(pipeline)
    result = {
        "deals": [], "investors": [], "partnerships": [], "won": [],
        "stages": {}, "off_stages": {}, "total_deals": 0, "total_investors": 0,
        "total_partnerships": 0, "total_won": 0,
        "total_value": 0, "weighted_value": 0, "stale_count": 0,
        "top_deals": [],
    }
    if not content:
        return result

    # Stage probability mapping (canonical stages)
    stage_prob = {
        "Lead": 0.05, "Qualified": 0.15, "Demo/POC": 0.30,
        "Proposal": 0.50, "Negotiation": 0.75, "Won": 1.0,
    }

    # Active Deals table
    deals = parse_md_table(content, r"##\s*Active Deals", source=str(pipeline))
    result["deals"] = deals
    result["total_deals"] = len(deals)

    total_value = 0
    weighted_value = 0
    stale_count = 0
    deal_weighted = []

    for d in deals:
        stage = d.get("Stage", "Unknown").strip()
        result["stages"][stage] = result["stages"].get(stage, 0) + 1

        val = parse_money(d.get("Est. Value", ""),
                          where=f"{pipeline} deal {d.get('Company', '?')!r}")
        total_value += val

        # A non-canonical stage is weighted at 5% and drawn under 'Other', and named.
        if stage not in stage_prob:
            result["off_stages"][stage] = result["off_stages"].get(stage, 0) + 1
            print(f"  Warning: deal {d.get('Company', '?')!r} has stage {stage!r}, "
                  f"which is not a canonical stage; weighted at 5% and drawn "
                  f"under 'Other'", file=sys.stderr)

        prob = stage_prob.get(stage, 0.05)
        w_val = val * prob
        weighted_value += w_val
        deal_weighted.append((d, w_val))

        # Stale detection: stage date > 14 days old
        stage_date_str = d.get("Stage Date", "").strip()
        if stage_date_str:
            try:
                sd = date.fromisoformat(stage_date_str)
                if (_today() - sd).days > 14:
                    stale_count += 1
            except ValueError:
                pass

    result["total_value"] = total_value
    result["weighted_value"] = weighted_value
    result["stale_count"] = stale_count

    # Top 3 deals by weighted value
    deal_weighted.sort(key=lambda x: -x[1])
    result["top_deals"] = [(d, w) for d, w in deal_weighted[:3]]

    # Investor Conversations
    investors = parse_md_table(content, r"##\s*Investor Conversations", source=str(pipeline))
    result["investors"] = investors
    result["total_investors"] = len(investors)

    # Partnership Discussions
    partnerships = parse_md_table(content, r"##\s*Partnership Discussions", source=str(pipeline))
    result["partnerships"] = partnerships
    result["total_partnerships"] = len(partnerships)

    # Won / Closed
    won = parse_md_table(content, r"##\s*Won\s*/\s*Closed", source=str(pipeline))
    result["won"] = won
    result["total_won"] = len(won)

    return result


_MONEY_RE = re.compile(r"^~?\$?\s*([0-9]+(?:\.[0-9]+)?)\s*([kmb])?$", re.IGNORECASE)
_MONEY_SCALE = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}


def parse_money(raw, where="pipeline"):
    """A deal value in whole units. Unparseable is 0 AND a warning.

    Accepts `$1.5M`, `2.5m`, `~500000` and `500k`. An unreadable value is 0,
    because inventing a number is worse, and it is named on stderr.
    """
    text = str(raw or "").replace(",", "").strip()
    if not text:
        return 0
    m = _MONEY_RE.match(text)
    if not m:
        print(f"  Warning: cannot read value {raw!r} in {where}; counted as 0",
              file=sys.stderr)
        return 0
    amount = float(m.group(1)) * _MONEY_SCALE.get((m.group(2) or "").lower(), 1)
    return int(amount)


def _as_int_or_count(value):
    """A count from a field whose shape the producer never promised.

    `odin-cadence.py --json` never promised a shape: a list is counted, a
    number is used, anything else is None (rendered as "-").
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, (list, tuple, set, dict)):
        return len(value)
    return None


def _parse_clock(raw_time):
    """A wall clock from a Time cell, or None when there isn't one.

    Accepts `9:30` and `9:30 AM` as well as `09:30`.
    """
    if not raw_time:
        return None
    m = re.match(r"\s*(\d{1,2}):(\d{2})\s*([APap][Mm])?", raw_time)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    meridiem = (m.group(3) or "").lower()
    if meridiem == "pm" and hour != 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return time(hour, minute)


def _as_percent(value):
    """A 0-100 completion percentage from an unvalidated producer field.

    `completion_rate` arrives unvalidated from the assistant-inbox `state.json`: a
    string, a fraction or a NaN must never render as a plausible percentage.

    A value in 0..1 is read as a fraction and scaled. That is a JUDGEMENT, not
    a fact the producer states, so it is made once, here, where it can be seen
    and argued with, rather than implied by a format string. 1.0 is ambiguous
    between "1%" and "100%" and is read as 100%, because a completion rate of
    exactly 1% is the less likely of the two by a wide margin.
    """
    if isinstance(value, bool) or value is None:
        return 0.0
    if isinstance(value, str):
        try:
            value = float(value.strip().rstrip("%"))
        except ValueError:
            print(f"[generate-dashboard] assistant-inbox completion_rate {value!r} is not "
                  f"a number; showing 0%", file=sys.stderr)
            return 0.0
    if not isinstance(value, (int, float)):
        print(f"[generate-dashboard] assistant-inbox completion_rate has type "
              f"{type(value).__name__}; showing 0%", file=sys.stderr)
        return 0.0
    # NaN and the infinities are floats, so the type check waves them through,
    # and the clamp below cannot refuse them: every comparison against NaN is
    # False, so `min` and `max` keep whichever operand they started with.
    if not math.isfinite(value):
        print(f"[generate-dashboard] assistant-inbox completion_rate is {value!r}, "
              f"which is not a finite number; showing 0%", file=sys.stderr)
        return 0.0
    if 0 < value <= 1:
        value *= 100
    return float(max(0.0, min(100.0, value)))


# A sync file older than this is reported as stale rather than read as fact.
# `/prime` has flagged these same two paths RED at 48 hours, under the literal
# label "SYNC STALE", since before this script existed.
SYNC_STALE_HOURS = 48


def sync_age_hours(sync_time):
    """Hours since a `> Synced: YYYY-MM-DD HH:MM [(Zone)]` stamp, or None.

    `sync-exchange` writes that stamp at the top of both files it produces.

    Returns None when the stamp is missing or unreadable, which the callers
    render as "age unknown" rather than as fresh. A stamp that cannot be read is
    not evidence of freshness.
    """
    if not sync_time:
        return None
    m = re.match(r"\s*(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})", str(sync_time))
    if not m:
        return None
    try:
        stamped = datetime(
            *(int(p) for p in m.group(1).split("-")),
            int(m.group(2)), int(m.group(3)), tzinfo=get_default_tz(),
        )
    except ValueError:
        # The regex matches the shape of a timestamp, not a real one.
        return None
    return (_now() - stamped).total_seconds() / 3600.0


def collect_calendar():
    """Parse upcoming.md for today's meetings."""
    content = read_file(calendar_file())
    result = {"meetings": [], "sync_time": "", "date_str": _today().strftime("%Y-%m-%d"),
              "source_read": False, "age_hours": None}

    if not content:
        # Unread, not empty: `build_bridge` says the source was not read.
        return result
    result["source_read"] = True

    # Extract sync time
    sync_match = re.search(r"Synced:\s*(.+)", content)
    if sync_match:
        result["sync_time"] = sync_match.group(1).strip()
        result["age_hours"] = sync_age_hours(result["sync_time"])

    # NO conversion: `upcoming.md` is already local (sync-exchange writes local
    # dates and times), so today's meetings are the rows under today's header.
    meetings = []
    for sec in re.finditer(r"##\s*(\d{4}-\d{2}-\d{2})", content):
        try:
            section_date = date.fromisoformat(sec.group(1))
        except ValueError:
            continue
        if section_date != _today():
            continue
        rest = content[sec.start():]
        nxt = re.search(r"\n##\s*\d{4}-\d{2}-\d{2}", rest[3:])
        section = rest[:nxt.start() + 3] if nxt else rest
        for m in parse_md_table(section, source=str(calendar_file())):
            raw_time = m.get("Time", "").strip()
            clock = _parse_clock(raw_time)
            if clock is None:
                # Kept and flagged, never dropped in silence. This is the one
                # panel where an absent row is the worst outcome: a meeting the
                # CEO does not know about costs more than a row that reads
                # oddly. The unparsed text stays in the cell so it is visible.
                if raw_time:
                    print(f"[generate-dashboard] calendar row kept with an "
                          f"unparsed Time {raw_time!r}", file=sys.stderr)
                    meetings.append(m)
                continue
            m["Time"] = clock.strftime("%H:%M")
            meetings.append(m)

    meetings.sort(key=lambda m: m.get("Time", ""))
    result["meetings"] = meetings
    return result


def collect_emails():
    """Parse inbox-latest.md for email summary."""
    content = read_file(email_file())
    result = {"emails": [], "sync_time": "", "count": 0,
              "source_read": False, "age_hours": None}

    if not content:
        # Same rule as the calendar beside it: an unread source is not an empty
        # inbox. "No recent emails" used to be printed either way.
        return result
    result["source_read"] = True

    sync_match = re.search(r"Synced:\s*(.+)", content)
    if sync_match:
        result["sync_time"] = sync_match.group(1).strip()
        result["age_hours"] = sync_age_hours(result["sync_time"])

    count_match = re.search(r"Count:\s*(\d+)", content)
    if count_match:
        result["count"] = int(count_match.group(1))

    emails = parse_md_table(content, source=str(email_file()))
    result["emails"] = emails
    return result


def _strip_md(text):
    """A table cell or bold line without its `**` emphasis."""
    return re.sub(r"\*\*", "", str(text or "")).strip()


def collect_strategy():
    """The heading, the live go-to-market phase and the stated indicators.

    All three come from the `strategy` record; anything it does not state is
    left empty, and the panel shows "not set" rather than a stale literal.
    """
    content = read_file(strategy_file())
    result = {"heading": "", "phase": "", "indicators": []}

    if not content:
        return result

    lines = content.split("\n")

    # Heading: the first bold line under "## Current Focus Period".
    in_focus = False
    for line in lines:
        if re.match(r"##(?!#)", line.strip()):
            in_focus = bool(re.match(r"##\s*Current Focus", line.strip(), re.IGNORECASE))
            continue
        if in_focus:
            m = re.match(r"\*\*(.+?)\*\*\s*$", line.strip())
            if m:
                result["heading"] = m.group(1).strip()
                break

    # Phase: the first go-to-market row whose timeline says it is live.
    for row in parse_md_table(content, r"##\s*Go-to-Market", source=str(strategy_file())):
        timeline = _strip_md(row.get("Timeline", ""))
        if re.search(r"\b(live|active)\b", timeline, re.IGNORECASE):
            phase = _strip_md(row.get("Phase", ""))
            region = _strip_md(row.get("Region", ""))
            result["phase"] = f"{phase}: {region} ({timeline})" if region else phase
            break

    # Indicators: the bullets under the "Indicators that confirm" line.
    in_ind = False
    for line in lines:
        stripped = line.strip()
        if re.search(r"Indicators that confirm", stripped, re.IGNORECASE):
            in_ind = True
            continue
        if not in_ind:
            continue
        if stripped.startswith("- "):
            result["indicators"].append(_strip_md(stripped[2:]))
        elif stripped and result["indicators"]:
            break
    return result


# Every figure section 06 renders, with the pattern that finds it in
# current-data.md. No figure is written in this file: the engine is public, and
# a default here is the operator's number on every clone (HOS-165).
_METRIC_PATTERNS = {
    "headcount": r"Headcount.*?(\d+\+?)",
    "hiring_target": r"Hiring target.*?\|\s*~?(\d+)",
    "dpi_tam_2030": r"Global market \(2030\)\s*\|\s*\*{0,2}(\$[\d.]+[KMB])",
    "cagr": r"Global market \(2030\)\s*\|[^|]*\|\s*\*{0,2}([\d.]+%)",
    # The home-region row's label is the company's own wording, so a data
    # repository may name its own pattern (`FIGURE_PATTERNS_FILE`).
    "cis_2030": r"Home[- ]region market[^|]*\|[^|]*?(\$[\d.]+[KMB])\s*\|",
    # `exited`, not a bare `(\d+) countries`. The loose form matched the
    # country count in the headcount row, thirty lines earlier in the file,
    # and would have printed that under the label "Incumbent Vacuum" - a NEW
    # wrong number introduced by the fix for a stale one. Caught by running
    # the pattern against the real file before shipping it.
    "predecessor_vacuum_countries": r"exited\s+(\d+)\s+countries",
}

#: A JSON object in the data repository's config directory mapping a figure
#: name above to the pattern that finds it in the company's own current-data.md,
#: in place of the default. Absent, the defaults stand.
FIGURE_PATTERNS_FILE = "dashboard-figure-patterns.json"


def metric_patterns() -> dict[str, str]:
    """`_METRIC_PATTERNS`, with the data repository's own patterns laid over it."""
    patterns = dict(_METRIC_PATTERNS)
    try:
        path = get_data_config_dir() / FIGURE_PATTERNS_FILE
        own = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return patterns
    except (OSError, UnicodeError, json.JSONDecodeError, DataPathError) as exc:
        print(f"  Warning: {FIGURE_PATTERNS_FILE} unreadable ({exc}); default patterns used",
              file=sys.stderr)
        return patterns
    if not isinstance(own, dict):
        print(f"  Warning: {FIGURE_PATTERNS_FILE} is not a JSON object; ignored", file=sys.stderr)
        return patterns
    for field, pattern in own.items():
        if field in patterns and isinstance(pattern, str):
            try:
                re.compile(pattern)
            except re.error as exc:
                print(f"  Warning: {FIGURE_PATTERNS_FILE} pattern for {field} does not compile "
                      f"({exc}); default kept", file=sys.stderr)
                continue
            patterns[field] = pattern
    return patterns


#: Every figure the page reads. The two with no pattern come only from the
#: readings record: `mea_2030` and `modules_live`.
_METRIC_FIELDS = (*_METRIC_PATTERNS, "mea_2030", "modules_live")

#: What the page shows for a figure nobody supplied. Never a number.
UNKNOWN_FIGURE = "n/a"


def metric_readings_file() -> Path:
    """The private record of figure readings, beside current-data.md.

    It holds the last known reading of each figure in `_METRIC_FIELDS`, as a
    JSON object of strings, and is the only source of the two figures
    current-data.md cannot answer. A public clone has none, and the page then
    says each figure is unknown rather than showing somebody's.
    """
    return data_path("dashboard-figures")


def collect_metrics():
    """The figures section 06 draws, read from current-data.md where they exist.

    Three layers, the later one winning: `UNKNOWN_FIGURE`, then the readings
    record (`metric_readings_file`), then current-data.md. No figure is written
    in this public file. A figure current-data.md does not answer is named on
    stderr, with the reading shown instead.

    `mea_2030` has no pattern on purpose. Its row lives in the geography table,
    whose cells carry a bare amount with no magnitude where the global table
    writes one, and that table states no unit anywhere. Extracting it would
    render a unitless figure under the label "MEA 2030". The page will not
    guess a unit its source does not state. `modules_live` appears nowhere in
    current-data.md.
    """
    result = dict.fromkeys(_METRIC_FIELDS, UNKNOWN_FIGURE)

    readings_path = metric_readings_file()
    try:
        readings = json.loads(readings_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        readings = {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"  Warning: {readings_path.name} unreadable ({exc}); "
              f"figures it holds show as {UNKNOWN_FIGURE!r}", file=sys.stderr)
        readings = {}
    if not isinstance(readings, dict):
        print(f"  Warning: {readings_path.name} is not a JSON object; ignored",
              file=sys.stderr)
        readings = {}
    for field in _METRIC_FIELDS:
        value = readings.get(field)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            result[field] = str(value)

    # Resolved once.
    metrics = metrics_file()
    content = read_file(metrics)
    if not content:
        return result

    for field, pattern in metric_patterns().items():
        m = re.search(pattern, content)
        if m:
            result[field] = m.group(1)
        else:
            print(f"  Warning: {field} not found in {metrics.name}; "
                  f"showing the reading {result[field]!r}",
                  file=sys.stderr)

    return result


def collect_freshness():
    """Check freshness markers on context files."""
    files_to_check = [
        # The helpers above: each file has its own registry name, so no single
        # directory addresses all of them.
        ("pipeline.md", pipeline_file()),
        ("current-data.md", metrics_file()),
        ("strategy.md", strategy_file()),
        ("people.md", get_people_file()),
    ]
    result = []
    for name, path in files_to_check:
        if not path.exists():
            result.append({"name": name, "date": None, "age": None, "health": "red"})
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            # One unreadable file degrades one row, never the whole run.
            print(f"  Warning: {name} is unreadable ({exc})", file=sys.stderr)
            result.append({"name": name, "date": None, "age": None,
                           "health": "gray"})
            continue
        match = re.search(r"Last verified:\s*(\d{4}-\d{2}-\d{2})", content)
        if match:
            date_str = match.group(1)
            try:
                verified = date.fromisoformat(date_str)
            except ValueError:
                # The regex matches the shape of a date; an impossible one degrades one row.
                print(f"  Warning: {name} has an impossible "
                      f"'Last verified: {date_str}'", file=sys.stderr)
                result.append({"name": name, "date": date_str, "age": None,
                               "health": "gray"})
                continue
            age = (_today() - verified).days
            health = "green" if age <= 7 else ("yellow" if age <= 14 else "red")
            result.append({"name": name, "date": date_str, "age": age, "health": health})
        else:
            result.append({"name": name, "date": None, "age": None, "health": "gray"})
    return result


def freshness_summary(freshness) -> str:
    """One line naming every band that is not green.

    `collect_freshness` returns four health values; "all current" means all
    green, never merely no red. A function so a test can call what runs.
    """
    stale = sum(1 for f in freshness if f["health"] == "red")
    ageing = sum(1 for f in freshness if f["health"] == "yellow")
    unmarked = sum(1 for f in freshness if f["health"] == "gray")
    if not (stale or ageing or unmarked):
        return f"all {len(freshness)} files current"
    parts = []
    if stale:
        parts.append(f"{stale} stale")
    if ageing:
        parts.append(f"{ageing} ageing")
    if unmarked:
        parts.append(f"{unmarked} with no readable marker")
    return f"{', '.join(parts)} of {len(freshness)} files"


def collect_hiring():
    """Parse hiring-pipeline.md for open roles and urgency."""
    # Resolved once, for the four uses below; see `collect_pipeline`.
    hiring = hiring_file()
    content = read_file(hiring)
    result = {"p1": [], "p2": [], "p3": [], "urgent": [], "total": 0}

    if not content:
        return result

    # Parse tables for each priority section
    p1 = parse_md_table(content, r"###\s*P1", source=str(hiring))
    p2 = parse_md_table(content, r"###\s*P2", source=str(hiring))
    p3 = parse_md_table(content, r"###\s*P3", source=str(hiring))

    result["p1"] = p1
    result["p2"] = p2
    result["p3"] = p3
    result["total"] = len(p1) + len(p2) + len(p3)

    # Find URGENT roles
    for role in p1 + p2 + p3:
        status = role.get("Status", "") + " " + role.get("Notes", "")
        if "URGENT" in status.upper():
            result["urgent"].append(role)

    return result


def collect_content_cadence():
    """Check recent content output for newsletter and LinkedIn."""
    result = {
        "newsletter_days": None, "newsletter_status": "NO DATA",
        "newsletter_last": None,
        "linkedin_count_week": 0, "linkedin_status": "NO DATA",
    }

    # Newsletter: check most recent dated directory in `newsletters`
    if newsletters_dir().exists():
        dated_dirs = []
        for d in newsletters_dir().iterdir():
            if d.is_dir() and re.match(r"\d{4}-\d{2}-\d{2}", d.name):
                try:
                    dt = date.fromisoformat(d.name)
                    dated_dirs.append(dt)
                except ValueError:
                    pass
        if dated_dirs:
            latest = max(dated_dirs)
            days_since = (_today() - latest).days
            result["newsletter_days"] = days_since
            result["newsletter_last"] = latest.strftime("%Y-%m-%d")
            result["newsletter_status"] = "ON TRACK" if days_since <= 7 else "BEHIND"

    # LinkedIn: count posts in the last 7 days, the archive included, because a
    # published post leaves `linkedin_dir()`; `git mv` keeps its mtime.
    week_ago = _today() - timedelta(days=7)
    linkedin_count = 0
    any_source = False
    for ldir, files in ((linkedin_dir(), "flat"),
                        (linkedin_archive_dir(), "deep")):
        if not ldir.exists():
            continue
        any_source = True
        # The archive nests one folder per slug, so a flat iterdir sees only
        # directories and counts nothing.
        entries = ldir.rglob("*.md") if files == "deep" else ldir.iterdir()
        for f in entries:
            if f.is_file():
                # Check by file modification date
                mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=get_default_tz()).date()
                if mtime >= week_ago:
                    linkedin_count += 1
    result["linkedin_count_week"] = linkedin_count
    # Only overwrite NO DATA when something was actually measured.
    if any_source:
        result["linkedin_status"] = "ON TRACK" if linkedin_count >= 2 else "BEHIND"

    return result


def collect_assistant_inbox():
    """Parse assistant-inbox tasks and state for summary."""
    result = {
        "active_total": 0, "p1": 0, "p2": 0, "p3": 0,
        "aging": 0, "completion_rate": 0.0,
        "tasks": [],
        # Whether each of the two sources was actually read. Without these the
        # panel could not tell "read it, the answer is zero" from "never opened
        # it", and rendered the second as the first.
        "tasks_read": False, "rate_known": False,
    }

    # Parse tasks.md for active items
    tasks_content = read_file(assistant_inbox_tasks_file())
    if tasks_content:
        result["tasks_read"] = True
        in_active = False
        for line in tasks_content.split("\n"):
            if re.match(r"##\s*Active", line, re.IGNORECASE):
                in_active = True
                continue
            # ANY other level-2 heading ends the Active section.
            if re.match(r"##(?!#)", line.strip()):
                in_active = False
                continue
            if in_active and line.strip().startswith("- [ ]"):
                result["active_total"] += 1
                # Extract priority
                p_match = re.search(r"`(P[123])`", line)
                if p_match:
                    p = p_match.group(1).lower()
                    result[p] = result.get(p, 0) + 1

                # Extract date for aging check (>3 days)
                date_match = re.search(r"\*\*(\d{4}-\d{2}-\d{2})\*\*", line)
                if date_match:
                    try:
                        task_date = date.fromisoformat(date_match.group(1))
                        if (_today() - task_date).days > 3:
                            result["aging"] += 1
                    except ValueError:
                        pass

                # Store task text for display
                text_match = re.search(r"`P[123]`\s*\|\s*(.+?)(?:\s*\||\s*$)", line)
                if text_match:
                    result["tasks"].append(text_match.group(1).strip())

    # Parse state.json for completion rate
    if assistant_inbox_state_file().exists():
        try:
            state = json.loads(assistant_inbox_state_file().read_text(encoding="utf-8"))
            stats = state.get("stats", {})
            result["completion_rate"] = _as_percent(stats.get("completion_rate"))
            result["rate_known"] = True
        except (json.JSONDecodeError, UnicodeError, OSError) as e:
            # Named, so a corrupt state.json never reads as a measured 0%.
            print(f"[generate-dashboard] assistant-inbox state.json unreadable "
                  f"({type(e).__name__}: {e}); completion rate unknown",
                  file=sys.stderr)

    return result


def collect_capture_payoff():
    """R10: daily /zk capture payoff. Counts signals captured in the last 7 days
    (knowledge notes + Odin episodes) and surfaces whether an episode cluster is
    ripe to promote to an Odin principle (reusing odin-cadence.py). Gives the
    weekly capture loop a DAILY surface so /zk capture visibly pays off.

    Degrades to {"available": False} when there is no Odin brain (e.g. a public
    clone with no data repository), so the panel hides rather than erroring."""
    if not odin_brain_dir().exists():
        return {"available": False}

    cutoff = _today() - timedelta(days=7)

    def _recent(md_path):
        try:
            fm, _ = _parse_fm(md_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            # Named, not swallowed: an unreadable note is not counted.
            print(f"[generate-dashboard] {md_path.name}: unreadable ({exc}); "
                  f"not counted as a captured signal", file=sys.stderr)
            return False
        unreadable = []
        for key in ("updated", "created", "date", "ingested"):
            val = fm.get(key)
            if not val:
                continue
            try:
                # A fallback chain (any recent field counts), through the shared coercion.
                if frontmatter_date(val) >= cutoff:
                    return True
            except ValueError:
                unreadable.append(f"{key}={val!r}")
                continue
        if unreadable:
            # Said, not swallowed. A note whose every date field is unreadable is
            # not counted, and the panel prints a number that looks measured.
            print(f"[generate-dashboard] {md_path.name}: no readable date "
                  f"({'; '.join(unreadable)}); not counted as a captured signal",
                  file=sys.stderr)
        return False

    signals = 0
    recent_titles = []
    # A child of the knowledge root another registry name owns (research surveys,
    # intelligence reports, the book library) holds no captured signal; the Odin
    # brain is the one such child this panel counts, by its own name.
    kroot = knowledge_dir()
    brain = odin_brain_dir()
    not_signals = claimed_children(kroot, ignore=("knowledge-base",))
    if brain.is_relative_to(kroot) and brain != kroot:
        not_signals -= {brain.relative_to(kroot).parts[0]}
    for md in kroot.rglob("*.md"):
        if md.name.lower() in ("index.md", "readme.md", "templates.md"):
            continue
        if md.relative_to(kroot).parts[0] in not_signals:
            continue
        if _recent(md):
            signals += 1
            if len(recent_titles) < 5:
                recent_titles.append(md.stem.replace("-", " "))

    promote_ready = last_collect = days_since = None
    # The shared reader checks the helper's exit code, so a crashed helper is
    # reported rather than read as a quiet week.
    cadence, cadence_error = read_cadence_json(
        WORKSPACE, script=ODIN_CADENCE_SCRIPT, timeout=30)
    if cadence_error:
        print(f"[generate-dashboard] odin cadence collect failed: {cadence_error}",
              file=sys.stderr)
    else:
        # Both fields come verbatim from `odin-cadence.py --json`; guarded.
        promote_ready = _as_int_or_count(cadence.get("reflect_clusters"))
        last_collect = cadence.get("last_collect")
        days_since = _as_int_or_count(cadence.get("days_since"))

    return {
        "available": True,
        "signals_week": signals,
        "recent_titles": recent_titles,
        "promote_ready": promote_ready,
        "last_collect": last_collect,
        "days_since": days_since,
        # None when the helper is absent (a legitimate blank), a reason string
        # when it ran and failed: nothing to show versus a broken read.
        "cadence_error": cadence_error,
    }


# ============================================================
# CSS
# ============================================================
def build_css(gt_light_b64="", gt_medium_b64=""):
    """Brand-compliant CSS for the Morning Dashboard.

    Embeds GT Standard fonts as base64 WOFF2 @font-face when provided.
    Falls back to Inter / system stack otherwise.

    Brand authority, both in the data repository: the registry name
    `corporate-style-guide`, and references/brand.css under `skill-brand-assets`
    for `design`
    """
    font_face = ""
    if gt_light_b64:
        font_face += f"""
@font-face {{
  font-family: 'GT Standard';
  src: url(data:font/woff2;base64,{gt_light_b64}) format('woff2');
  font-weight: 300;
  font-style: normal;
  font-display: swap;
}}"""
    if gt_medium_b64:
        font_face += f"""
@font-face {{
  font-family: 'GT Standard';
  src: url(data:font/woff2;base64,{gt_medium_b64}) format('woff2');
  font-weight: 500;
  font-style: normal;
  font-display: swap;
}}"""

    return render_template("dashboard.css", FONT_FACE=font_face)


# ============================================================
# Rendering / HTML Section Builders
# ============================================================
def build_cover(white_logo_b64):
    """Dark Palantinate-Blue / black cover page with white logo and orange corner.
    Brand spec: the registry name `corporate-style-guide` in the data repository
    (Signature Brand Elements).
    """
    date_long = _now().strftime("%A, %B %d, %Y")
    time_str = _now().strftime("%H:%M")
    zone_suffix = _zone_suffix()
    logo_html = (
        f'<img class="cover-logo" src="{white_logo_b64}" alt="{_CO_ALT}"/>'
        if white_logo_b64 else ''
    )
    return f"""
<div class="cover">
  <div class="cover-corner"></div>
  <div class="cover-accent"></div>
  <div class="cover-inner">
    {logo_html}
    <div class="cover-eyebrow">CEO Morning Dashboard</div>
    <div class="cover-title">Heading<span class="one-blue">.</span>State<span class="one-blue">.</span>Drift<span class="one-blue">.</span></div>
    <div class="cover-date">{esc(date_long)}</div>
    <div class="cover-meta">Generated {esc(time_str)}{zone_suffix} &middot; Internal &mdash; CEO Eyes Only</div>
  </div>
  <div class="cover-footer">
    <div class="cover-footer-marks"><span class="sq blue"></span><span class="sq orange"></span></div>
    <span>{(_CO_CREDIT + ' &middot; ') if _CO_CREDIT else ''}Proprietary &amp; Confidential</span>
  </div>
</div>
"""


def build_header(logo_b64):
    date_long = _now().strftime("%A, %B %d, %Y")
    time_str = _now().strftime("%H:%M")
    zone_suffix = _zone_suffix()
    return f"""
<div class="topbar">
  <div class="topbar-left">
    {'<img class="logo-img" src="' + logo_b64 + '" alt="Logo"/>' if logo_b64 else ''}
    <div class="pulse"></div>
    <span class="topbar-title">Morning Dashboard</span>
  </div>
  <div class="topbar-right">Internal &mdash; CEO Eyes Only</div>
</div>
<div class="datebar">
  <div class="datebar-date">{esc(date_long)}</div>
  <div class="datebar-meta">Generated {esc(time_str)}{zone_suffix}</div>
</div>
"""


def build_urgent(crm):
    items_html = []

    # Overdue commitments
    overdue = [c for c in crm["commitments_due"] if c["overdue"]]
    for c in overdue:
        items_html.append(f"""
<div class="alert-card">
  <div class="alert-label">Overdue Commitment</div>
  <div class="alert-text"><strong>{esc(c['name'])}</strong> ({esc(c['company'])})</div>
  <div class="alert-sub">{esc(c['text'])}</div>
</div>""")

    # RED contacts
    for c in crm["red"][:8]:
        days_str = f"{c['days_since']} days ago" if c.get("days_since") is not None else "no recorded touch"
        items_html.append(f"""
<div class="alert-card">
  <div class="alert-label">Relationship Overdue</div>
  <div class="alert-text"><strong>{esc(c['name'])}</strong> ({esc(c.get('company', ''))}) &mdash; {esc(c.get('type', ''))}</div>
  <div class="alert-sub">Last touch: {esc(days_str)} &bull; Cadence: {esc(c.get('cadence', '?'))} days</div>
</div>""")

    # Upcoming commitments (not overdue)
    upcoming = [c for c in crm["commitments_due"] if not c["overdue"]]
    for c in upcoming[:5]:
        items_html.append(f"""
<div class="alert-card warn">
  <div class="alert-label">Commitment Due</div>
  <div class="alert-text"><strong>{esc(c['name'])}</strong> ({esc(c['company'])})</div>
  <div class="alert-sub">{esc(c['text'])} &bull; Due: {esc(c['due'])}</div>
</div>""")

    if crm.get("failed"):
        # "All Clear" must be unreachable from an error path.
        items_html.append(f"""
<div class="alert-card">
  <div class="alert-label">CRM Data Unavailable</div>
  <div class="alert-text">The CRM scan failed, so this panel is EMPTY, not clear.</div>
  <div class="alert-sub">{esc(crm.get("failed"))}</div>
</div>""")
    elif not items_html:
        items_html.append("""
<div class="alert-card ok">
  <div class="alert-label">All Clear</div>
  <div class="alert-text">No urgent items this morning. Steady as she goes.</div>
</div>""")

    return f"""
<div class="section">
  <div class="section-num">01</div>
  <div class="section-title">Urgent Items</div>
  {"".join(items_html)}
</div>
"""


# How many of the synced emails the table shows. Named, because the number has
# to reach the label that admits to the cut.
EMAIL_PREVIEW_ROWS = 6


def _sync_label(name, source):
    """The panel's source line: when it synced, and whether that is stale."""
    if not source.get("source_read"):
        return (f'<div class="sync-label" style="color:var(--red);">{esc(name)} '
                f'&bull; NOT SYNCED</div>')
    age = source.get("age_hours")
    stamp = esc(source.get("sync_time") or "no timestamp")
    if age is None:
        return f'<div class="sync-label">{esc(name)} &bull; {stamp} (age unknown)</div>'
    if age >= SYNC_STALE_HOURS:
        return (f'<div class="sync-label" style="color:var(--red);">{esc(name)} '
                f'&bull; {stamp} &bull; STALE ({age / 24:.0f}d old)</div>')
    return f'<div class="sync-label">{esc(name)} &bull; {stamp}</div>'


def _empty_row(source, span, empty_text):
    """The row shown when a table has nothing in it.

    An unread source and an empty one are different facts and say different things.
    """
    if not source.get("source_read"):
        return (f'<tr><td colspan="{span}" style="color:var(--red);font-style:italic;">'
                f'Source not synced &mdash; this panel is EMPTY, not clear.</td></tr>')
    return (f'<tr><td colspan="{span}" style="color:var(--ink35);font-style:italic;">'
            f'{empty_text}</td></tr>')


def build_bridge(calendar, emails):
    # Calendar table
    cal_rows = ""
    for m in calendar["meetings"]:
        time_val = m.get("Time", "")
        subject = m.get("Subject", "")
        duration = m.get("Duration", "")
        cal_rows += f"<tr><td>{esc(time_val)}</td><td>{esc(subject)}</td><td>{esc(duration)}</td></tr>\n"

    if not cal_rows:
        cal_rows = _empty_row(calendar, 3, "No meetings scheduled today")

    cal_sync = _sync_label("Calendar", calendar)

    # Email table
    email_rows = ""
    for e in emails["emails"][:EMAIL_PREVIEW_ROWS]:
        from_val = e.get("From", "")
        subject = e.get("Subject", "")
        read = e.get("Read", "")
        dot = '<span style="color:var(--orange);">&#9679;</span> ' if read.lower() == "no" else ""
        email_rows += f"<tr><td>{dot}{esc(from_val)}</td><td>{esc(subject[:50])}</td></tr>\n"

    # The cut, admitted: say how many synced emails the table leaves out.
    shown = min(len(emails["emails"]), EMAIL_PREVIEW_ROWS)
    if len(emails["emails"]) > EMAIL_PREVIEW_ROWS:
        email_rows += (f'<tr><td colspan="2" style="color:var(--ink35);font-style:italic;">'
                       f'Showing {shown} of {len(emails["emails"])} synced'
                       f'</td></tr>\n')

    if not email_rows:
        email_rows = _empty_row(emails, 2, "No recent emails")

    email_sync = _sync_label("Email", emails)

    return f"""
<div class="section">
  <div class="section-num">02</div>
  <div class="section-title">Today's Bridge</div>
  <div class="two-col">
    <div class="col-left">
      {cal_sync}
      <table class="dtable">
        <thead><tr><th>Time</th><th>Subject</th><th>Duration</th></tr></thead>
        <tbody>{cal_rows}</tbody>
      </table>
    </div>
    <div class="col-right">
      {email_sync}
      <table class="dtable">
        <thead><tr><th>From</th><th>Subject</th></tr></thead>
        <tbody>{email_rows}</tbody>
      </table>
    </div>
  </div>
</div>
"""


def build_pipeline(pipeline):
    # Bar chart -- canonical pipeline stages in funnel order
    raw_stages = pipeline["stages"]
    canonical_order = ["Lead", "Qualified", "Demo/POC", "Proposal", "Negotiation", "Won"]

    # Off-list stages get an "Other" bar so the chart sums to Active Deals.
    off_total = sum(pipeline.get("off_stages", {}).values())
    bar_stages = canonical_order + (["Other"] if off_total else [])
    counts = {s: raw_stages.get(s, 0) for s in canonical_order}
    counts["Other"] = off_total

    if not raw_stages:
        bars_html = '<div style="color:var(--ink35);font-style:italic;">No pipeline data available</div>'
    else:
        max_count = max(counts[s] for s in bar_stages)
        if max_count == 0:
            max_count = 1
        bars = []
        for stage in bar_stages:
            count = counts[stage]
            height_pct = max(10, int((count / max_count) * 100)) if count > 0 else 4
            bars.append(f"""
<div class="bar-item">
  <div class="bar-count">{count}</div>
  <div class="bar-fill" style="height:{height_pct}%;"></div>
  <div class="bar-label">{esc(stage)}</div>
</div>""")
        bars_html = f'<div class="bar-chart">{"".join(bars)}</div>'

    # Format currency values
    total_val = pipeline.get("total_value", 0)
    weighted_val = pipeline.get("weighted_value", 0)
    stale = pipeline.get("stale_count", 0)
    total_str = f"${total_val / 1_000_000:.1f}M" if total_val >= 1_000_000 else f"${total_val:,.0f}"
    weighted_str = f"${weighted_val / 1_000_000:.1f}M" if weighted_val >= 1_000_000 else f"${weighted_val:,.0f}"

    stale_cls = "danger" if stale > 5 else ("accent" if stale > 0 else "up")

    # Top 3 deals by weighted value
    top_deals_html = ""
    if pipeline.get("top_deals"):
        top_rows = ""
        for deal, wval in pipeline["top_deals"]:
            company = deal.get("Company", "Unknown")
            stage = deal.get("Stage", "")
            w_str = f"${wval / 1_000_000:.1f}M" if wval >= 1_000_000 else f"${wval:,.0f}"
            top_rows += f"<tr><td><strong>{esc(company)}</strong></td><td>{esc(stage)}</td><td style='text-align:right;'>{esc(w_str)}</td></tr>\n"
        top_deals_html = f"""
<div class="sync-label" style="margin-top:16px;">Top 3 by Weighted Value</div>
<table class="dtable">
  <thead><tr><th>Company</th><th>Stage</th><th style="text-align:right;">Weighted</th></tr></thead>
  <tbody>{top_rows}</tbody>
</table>"""

    # Metrics strip
    return f"""
<div class="section">
  <div class="section-num">03</div>
  <div class="section-title">Pipeline Pulse</div>
  {bars_html}
  <div class="metrics-strip">
    <div class="metric-box">
      <div class="metric-val accent">{pipeline['total_deals']}</div>
      <div class="metric-label">Active Deals</div>
    </div>
    <div class="metric-box">
      <div class="metric-val accent">{esc(total_str)}</div>
      <div class="metric-label">Total Value</div>
    </div>
    <div class="metric-box">
      <div class="metric-val up">{esc(weighted_str)}</div>
      <div class="metric-label">Weighted Value</div>
    </div>
    <div class="metric-box">
      <div class="metric-val {stale_cls}">{stale}</div>
      <div class="metric-label">Stale (&gt;14d)</div>
    </div>
    <div class="metric-box">
      <div class="metric-val up">{pipeline['total_won']}</div>
      <div class="metric-label">Won</div>
    </div>
    <div class="metric-box">
      <div class="metric-val">{pipeline['total_investors']}</div>
      <div class="metric-label">Investor Talks</div>
    </div>
    <div class="metric-box">
      <div class="metric-val">{pipeline['total_partnerships']}</div>
      <div class="metric-label">Partnerships</div>
    </div>
  </div>
  {top_deals_html}
</div>
"""


def build_radar(crm):
    red_count = len(crm["red"])
    yellow_count = len(crm["yellow"])
    green_count = len(crm["green"])
    gray_count = len(crm["gray"])

    # The fourth bucket: contacts whose health could not be read, drawn only
    # when non-zero. Inline style rather than a class, because the stylesheet
    # lives in the private data overlay and a class would need a matching edit there.
    gray_circle = ""
    if gray_count:
        gray_circle = f"""
    <div class="radar-circle" style="background:#f7f7f7;border:1px solid #dcdcdc;">
      <div class="radar-num" style="color:var(--ink55);">{gray_count}</div>
      <div class="radar-lbl" style="color:var(--ink55);">Health Unreadable</div>
    </div>"""

    # RED contact details
    red_rows = ""
    for c in crm["red"][:10]:
        days_str = f"{c['days_since']}d" if c.get("days_since") is not None else "N/A"
        red_rows += f"<tr><td><strong>{esc(c['name'])}</strong></td><td>{esc(c.get('company',''))}</td><td>{esc(c.get('type',''))}</td><td style='color:var(--red);'>{esc(days_str)}</td></tr>\n"

    # YELLOW contact details
    yellow_rows = ""
    for c in crm["yellow"][:8]:
        days_str = f"{c['days_since']}d" if c.get("days_since") is not None else "N/A"
        yellow_rows += f"<tr><td><strong>{esc(c['name'])}</strong></td><td>{esc(c.get('company',''))}</td><td>{esc(c.get('type',''))}</td><td style='color:var(--yellow);'>{esc(days_str)}</td></tr>\n"

    contacts_table = ""
    if red_rows:
        contacts_table += f"""
<div class="sync-label" style="margin-top:16px;">Overdue &mdash; Need Attention</div>
<table class="dtable">
  <thead><tr><th>Name</th><th>Company</th><th>Type</th><th>Since</th></tr></thead>
  <tbody>{red_rows}</tbody>
</table>"""

    if yellow_rows:
        contacts_table += f"""
<div class="sync-label" style="margin-top:16px;">Approaching &mdash; Watch</div>
<table class="dtable">
  <thead><tr><th>Name</th><th>Company</th><th>Type</th><th>Since</th></tr></thead>
  <tbody>{yellow_rows}</tbody>
</table>"""

    return f"""
<div class="section">
  <div class="section-num">04</div>
  <div class="section-title">Relationship Radar</div>
  <div class="radar-row">
    <div class="radar-circle r">
      <div class="radar-num">{red_count}</div>
      <div class="radar-lbl">Overdue</div>
    </div>
    <div class="radar-circle y">
      <div class="radar-num">{yellow_count}</div>
      <div class="radar-lbl">Approaching</div>
    </div>
    <div class="radar-circle g">
      <div class="radar-num">{green_count}</div>
      <div class="radar-lbl">On Track</div>
    </div>{gray_circle}
  </div>
  {contacts_table}
</div>
"""


#: What the Heading Check shows for anything the strategy record does not state.
STRATEGY_NOT_SET = "not set"


def build_heading(strategy, pipeline, metrics, hiring):
    # Every indicator's state is computed from the data its caption quotes.
    indicators = []

    # 1. Revenue conversion
    won = pipeline["total_won"]
    rev_state = "g" if won >= 2 else ("y" if won >= 1 else "r")
    rev_status = f"{won} won" if won else "Pre-revenue"
    indicators.append(("Revenue Conversion", rev_state, rev_status))

    # 2. Partner channel activation
    active_partners = sum(1 for p in pipeline["partnerships"] if p.get("Stage", "").lower() == "active")
    partner_state = "g" if active_partners >= 3 else ("y" if active_partners >= 1 else "r")
    indicators.append(("Partner Channel Activation", partner_state, f"{active_partners} active"))

    # 3. Hiring momentum. An unfilled URGENT role is the drag this indicator
    # shows, and the status names the same numbers the state is read from.
    open_roles = hiring["total"]
    urgent_roles = len(hiring["urgent"])
    hire_state = "r" if urgent_roles else ("y" if open_roles else "g")
    # Plain text, no HTML entity: this string goes through `esc()` at the render
    # below, which would turn a `&bull;` into the literal characters "&bull;".
    hire_status = (f"{metrics['headcount']} of {metrics['hiring_target']}, "
                   f"{open_roles} open, {urgent_roles} urgent")
    indicators.append(("Hiring Momentum", hire_state, hire_status))

    # 4. Fundraising progress, on the same bands as Partner Channel Activation
    # above. Zero live investor conversations is a red state on this page.
    investors_active = pipeline["total_investors"]
    fund_state = "g" if investors_active >= 3 else ("y" if investors_active >= 1 else "r")
    indicators.append(("Fundraising Progress", fund_state, f"{investors_active} conversations"))

    items = ""
    for label, state, status in indicators:
        items += f"""
<li class="heading-item">
  <div class="heading-dot {state}"></div>
  <div class="heading-text">{esc(label)}</div>
  <div class="heading-status" style="color:var(--{'green' if state == 'g' else 'yellow' if state == 'y' else 'red'});">{esc(status)}</div>
</li>"""

    heading_text = strategy.get("heading") or STRATEGY_NOT_SET
    phase_text = strategy.get("phase") or STRATEGY_NOT_SET

    stated = strategy.get("indicators") or []
    if stated:
        stated_items = "".join(f"<li>{esc(i)}</li>" for i in stated)
        stated_html = (f'<div class="sync-label" style="margin-top:16px;">Stated Indicators (strategy)</div>'
                       f'<ul style="margin:6px 0 0 18px;font-size:12px;">{stated_items}</ul>')
    else:
        stated_html = (f'<div class="sync-label" style="margin-top:16px;">Stated Indicators (strategy): '
                       f'{STRATEGY_NOT_SET}</div>')

    return f"""
<div class="section">
  <div class="section-num">05</div>
  <div class="section-title">Heading Check</div>
  <div style="display:flex;gap:16px;margin-bottom:16px;">
    <div style="flex:1;padding:12px 16px;background:var(--orlight);border-left:3px solid var(--orange);border-radius:0 4px 4px 0;">
      <div class="sync-label" style="margin-bottom:2px;">Current Heading</div>
      <div style="font-size:16px;font-weight:600;">{esc(heading_text)}</div>
    </div>
    <div style="padding:12px 16px;background:var(--ink);border-radius:4px;min-width:160px;text-align:center;">
      <div style="font-family:'IBM Plex Mono',monospace;font-size:7.5px;letter-spacing:1.5px;text-transform:uppercase;color:rgba(255,255,255,0.4);margin-bottom:2px;">Go-To-Market</div>
      <div style="font-family:'Bebas Neue',sans-serif;font-size:16px;color:var(--orange);">{esc(phase_text)}</div>
    </div>
  </div>
  <ul class="heading-list">{items}</ul>
  {stated_html}
</div>
"""


def build_market(metrics):
    return f"""
<div class="section" style="padding-top:20px;padding-bottom:20px;">
  <div class="section-num">06</div>
  <div class="section-title">Market Context</div>
  <div class="market-strip">
    <div class="market-item">
      <div class="market-val">{esc(metrics['dpi_tam_2030'])}</div>
      <div class="market-label">Global Market 2030</div>
    </div>
    <div class="market-item">
      <div class="market-val">{esc(metrics['mea_2030'])}</div>
      <div class="market-label">MEA 2030</div>
    </div>
    <div class="market-item">
      <div class="market-val">{esc(metrics['cis_2030'])}</div>
      <div class="market-label">Home Region 2030</div>
    </div>
    <div class="market-item">
      <div class="market-val">{esc(metrics['predecessor_vacuum_countries'])}</div>
      <div class="market-label">Incumbent Vacuum</div>
    </div>
    <div class="market-item">
      <div class="market-val">{esc(metrics['cagr'])}</div>
      <div class="market-label">CAGR</div>
    </div>
  </div>
</div>
"""


def build_freshness(freshness):
    rows = ""
    for f in freshness:
        dot_cls = {"green": "g", "yellow": "y", "red": "r"}.get(f["health"], "gray")
        age_str = f"{f['age']}d ago" if f["age"] is not None else "unknown"
        date_str = f["date"] if f["date"] else "no marker"
        rows += f"""
<div class="fresh-row">
  <div class="fresh-dot {dot_cls}"></div>
  <div class="fresh-name">{esc(f['name'])}</div>
  <div class="fresh-age">{esc(date_str)}</div>
  <div class="fresh-age">{esc(age_str)}</div>
</div>"""

    return f"""
<div class="section" style="padding-top:16px;padding-bottom:16px;">
  <div class="section-num">07</div>
  <div class="section-title">Data Freshness</div>
  {rows}
</div>
"""


def build_hiring(hiring):
    p1_count = len(hiring["p1"])
    p2_count = len(hiring["p2"])
    p3_count = len(hiring["p3"])
    total = hiring["total"]
    urgent = hiring["urgent"]

    # Urgent roles alert
    urgent_html = ""
    if urgent:
        for role in urgent:
            role_name = role.get("Role", "Unknown")
            status = role.get("Status", "")
            urgent_html += f"""
<div class="alert-card">
  <div class="alert-label">Urgent Hire</div>
  <div class="alert-text"><strong>{esc(role_name)}</strong></div>
  <div class="alert-sub">Status: {esc(status)}</div>
</div>"""

    if total == 0:
        body = '<div style="color:var(--ink35);font-style:italic;">No hiring pipeline data available</div>'
    else:
        body = f"""
{urgent_html}
<div class="metrics-strip">
  <div class="metric-box">
    <div class="metric-val danger">{p1_count}</div>
    <div class="metric-label">P1 Critical</div>
  </div>
  <div class="metric-box">
    <div class="metric-val accent">{p2_count}</div>
    <div class="metric-label">P2 High</div>
  </div>
  <div class="metric-box">
    <div class="metric-val">{p3_count}</div>
    <div class="metric-label">P3 Planned</div>
  </div>
  <div class="metric-box">
    <div class="metric-val accent">{total}</div>
    <div class="metric-label">Total Open</div>
  </div>
</div>"""

    return f"""
<div class="section">
  <div class="section-num">08</div>
  <div class="section-title">Hiring Pipeline</div>
  {body}
</div>
"""


def build_content_cadence(cadence):
    nl_days = cadence["newsletter_days"]
    nl_status = cadence["newsletter_status"]
    nl_last = cadence["newsletter_last"]
    li_count = cadence["linkedin_count_week"]
    li_status = cadence["linkedin_status"]

    # Newsletter indicator
    if nl_days is not None:
        nl_color = "var(--green)" if nl_status == "ON TRACK" else "var(--red)"
        nl_detail = f"Last issue: {esc(nl_last)} ({nl_days}d ago)"
    else:
        nl_color = "var(--ink35)"
        nl_detail = "No newsletter issues found"
        nl_status = "NO DATA"

    # LinkedIn indicator, on the same three states as the newsletter beside it.
    if li_status == "NO DATA":
        li_color = "var(--ink35)"
        li_detail = "No LinkedIn content directory found"
    else:
        li_color = "var(--green)" if li_status == "ON TRACK" else "var(--red)"
        li_detail = f"{li_count} posts/drafts this week"

    # The dots band the same three states the captions do; NO DATA is gray, never red.
    def _dot(status):
        if status == "ON TRACK":
            return "g"
        return "gray" if status == "NO DATA" else "r"

    nl_dot = _dot(nl_status)
    li_dot = _dot(li_status)

    return f"""
<div class="section">
  <div class="section-num">09</div>
  <div class="section-title">Content Cadence</div>
  <div style="display:flex;gap:16px;">
    <div style="flex:1;padding:14px 16px;border:1px solid var(--ink12);border-radius:4px;">
      <div class="sync-label">Newsletter (Target: Weekly)</div>
      <div style="display:flex;align-items:center;gap:10px;margin-top:6px;">
        <div class="heading-dot {nl_dot}"></div>
        <span style="font-family:'IBM Plex Mono',monospace;font-size:11px;letter-spacing:1px;text-transform:uppercase;color:{nl_color};font-weight:700;">{esc(nl_status)}</span>
      </div>
      <div class="alert-sub" style="margin-top:6px;">{nl_detail}</div>
    </div>
    <div style="flex:1;padding:14px 16px;border:1px solid var(--ink12);border-radius:4px;">
      <div class="sync-label">LinkedIn (Target: 2+/week)</div>
      <div style="display:flex;align-items:center;gap:10px;margin-top:6px;">
        <div class="heading-dot {li_dot}"></div>
        <span style="font-family:'IBM Plex Mono',monospace;font-size:11px;letter-spacing:1px;text-transform:uppercase;color:{li_color};font-weight:700;">{esc(li_status)}</span>
      </div>
      <div class="alert-sub" style="margin-top:6px;">{esc(li_detail)}</div>
    </div>
  </div>
</div>
"""


def build_assistant_inbox(assistant_tasks):
    active = assistant_tasks["active_total"]
    p1 = assistant_tasks["p1"]
    p2 = assistant_tasks["p2"]
    p3 = assistant_tasks["p3"]
    aging = assistant_tasks["aging"]
    rate = assistant_tasks["completion_rate"]

    tasks_read = bool(assistant_tasks.get("tasks_read"))
    rate_known = bool(assistant_tasks.get("rate_known"))

    # The two sources are banded separately: an empty in-tray is a result, a
    # file nobody opened is not. Only when NEITHER was read is there nothing to draw.
    if not tasks_read and not rate_known:
        body = '<div style="color:var(--ink35);font-style:italic;">No assistant-inbox tasks data available</div>'
    else:
        # The counts all come from tasks.md, so they are dashes when it was not
        # read - the same rule `rate_str` follows one file over, and the same
        # rule `_empty_row` follows two panels up. A "0" here would be a
        # measurement nobody took.
        def _count(n):
            return str(n) if tasks_read else "-"

        aging_cls = ("danger" if aging > 5 else ("accent" if aging > 0 else "up")) \
            if tasks_read else ""
        # "0%" is a measurement. An unreadable state.json is not one, and drew
        # the same three characters.
        rate_str = f"{rate:.0f}%" if rate_known else "-"
        # Named on the page, not inferred from a row of dashes.
        unread_note = "" if tasks_read else (
            '<div style="color:var(--red);font-style:italic;margin-top:8px;">'
            'Assistant-inbox tasks.md not read: the task counts above are '
            'unknown, not zero.</div>')

        body = f"""
<div class="metrics-strip">
  <div class="metric-box">
    <div class="metric-val accent">{_count(active)}</div>
    <div class="metric-label">Active Tasks</div>
  </div>
  <div class="metric-box">
    <div class="metric-val danger">{_count(p1)}</div>
    <div class="metric-label">P1 Tasks</div>
  </div>
  <div class="metric-box">
    <div class="metric-val accent">{_count(p2)}</div>
    <div class="metric-label">P2 Tasks</div>
  </div>
  <div class="metric-box">
    <div class="metric-val">{_count(p3)}</div>
    <div class="metric-label">P3 Tasks</div>
  </div>
  <div class="metric-box">
    <div class="metric-val {aging_cls}">{_count(aging)}</div>
    <div class="metric-label">Aging (&gt;3d)</div>
  </div>
  <div class="metric-box">
    <div class="metric-val">{rate_str}</div>
    <div class="metric-label">Completion</div>
  </div>
</div>{unread_note}"""

    return f"""
<div class="section">
  <div class="section-num">10</div>
  <div class="section-title">Assistant Inbox Tasks</div>
  {body}
</div>
"""


def build_capture_payoff(payoff):
    """R10: the daily capture-payoff panel. Hidden entirely when no Odin brain."""
    if not payoff.get("available"):
        return ""

    signals = payoff.get("signals_week", 0)
    promote = payoff.get("promote_ready")
    days_since = payoff.get("days_since")
    cadence_error = payoff.get("cadence_error")

    sig_cls = "up" if signals > 0 else ""
    # "?" and not "-" when the cadence read FAILED; a dash is the legitimate
    # blank for a workspace with no cadence helper.
    unknown = "?" if cadence_error else "-"
    unread = bool(cadence_error)
    promote_val = unknown if promote is None else str(promote)
    if unread and promote is None:
        promote_cls = "danger"
    else:
        promote_cls = "accent" if (promote or 0) > 0 else ""
    collect_val = unknown if days_since is None else f"{days_since}d"
    if unread and days_since is None:
        collect_cls = "danger"
    else:
        collect_cls = "danger" if (days_since is not None and days_since >= 7) else ""

    titles = payoff.get("recent_titles") or []
    if titles:
        recent = '<div style="margin-top:12px;color:var(--ink55);font-size:12px;">Recent: ' \
                 + esc(", ".join(titles)) + "</div>"
    else:
        recent = '<div style="margin-top:12px;color:var(--ink35);font-style:italic;font-size:12px;">' \
                 'No captures in the last 7 days &mdash; a quiet week for /zk.</div>'

    if promote and promote > 0:
        nudge = f'<div style="margin-top:10px;color:var(--accent);font-size:12px;">' \
                f'{promote} episode cluster(s) ripe to promote &mdash; run <code>/odin reflect</code>.</div>'
    else:
        nudge = ""

    # Named on the page, not only on stderr. The CEO reads the rendered file,
    # often hours after the run, and a cadence read that failed silently is a
    # nudge that did not fire: the "Since Last Harvest" box turns red at 7 days
    # and a dead helper never turned it any colour at all.
    if cadence_error:
        failure = ('<div style="margin-top:10px;color:var(--red);font-size:12px;">'
                   'Odin cadence unread: ' + esc(cadence_error)
                   + '. Clusters and last-harvest figures above are unknown, '
                     'not zero.</div>')
    else:
        failure = ""

    body = f"""
<div class="metrics-strip">
  <div class="metric-box">
    <div class="metric-val {sig_cls}">{signals}</div>
    <div class="metric-label">Signals Captured (7d)</div>
  </div>
  <div class="metric-box">
    <div class="metric-val {promote_cls}">{promote_val}</div>
    <div class="metric-label">Clusters to Promote</div>
  </div>
  <div class="metric-box">
    <div class="metric-val {collect_cls}">{collect_val}</div>
    <div class="metric-label">Since Last Harvest</div>
  </div>
</div>
{recent}
{nudge}{failure}"""

    return f"""
<div class="section">
  <div class="section-num">11</div>
  <div class="section-title">Capture Payoff &mdash; /zk &amp; Odin</div>
  {body}
</div>
"""


def build_footer():
    return f"""
<div class="footer">
  <div class="footer-marks"><span class="sq blue"></span><span class="sq orange"></span></div>
  <div class="footer-left">Morning Dashboard &middot; Generated {esc(_now().strftime("%Y-%m-%d %H:%M"))}{(' &middot; ' + _CO_CREDIT) if _CO_CREDIT else ''}</div>
  <div class="footer-right">Internal &mdash; CEO Eyes Only</div>
</div>
"""


# ============================================================
# CLI / Main
# ============================================================
def generate_html(crm, pipeline, calendar, emails, strategy, metrics, freshness,
                   hiring, content_cadence, assistant_tasks, capture_payoff=None):
    white_logo_b64 = load_logo_base64(logo_white_path())
    gt_light_b64 = load_font_b64(gt_light_font())
    gt_medium_b64 = load_font_b64(gt_medium_font())
    css = build_css(gt_light_b64, gt_medium_b64)

    cover = build_cover(white_logo_b64)
    sections = [
        # Topbar sits on dark ink background -> use white logo.
        build_header(white_logo_b64),
        build_urgent(crm),
        build_bridge(calendar, emails),
        build_pipeline(pipeline),
        build_radar(crm),
        build_heading(strategy, pipeline, metrics, hiring),
        build_market(metrics),
        build_freshness(freshness),
        build_hiring(hiring),
        build_content_cadence(content_cadence),
        build_assistant_inbox(assistant_tasks),
        build_capture_payoff(capture_payoff or {}),
        build_footer(),
    ]

    # Inter fallback (Google Fonts) only loaded if GT Standard embed failed.
    inter_link = ""
    if not gt_light_b64 or not gt_medium_b64:
        inter_link = (
            '<link rel="preconnect" href="https://fonts.googleapis.com"/>\n'
            '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>\n'
            '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;700&display=swap" rel="stylesheet"/>'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Morning Dashboard &mdash; {esc(_today().strftime("%Y-%m-%d"))}</title>
{inter_link}
<style>{css}</style>
</head>
<body>
{cover}
<div class="page">
{"".join(sections)}
</div>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(description="CEO Morning Dashboard Generator")
    parser.add_argument("--output-dir", help="Custom output directory")
    parser.add_argument("--pdf", action="store_true", help="Also generate PDF via html-to-pdf.py")
    args = parser.parse_args()

    # Determine output directory
    out_dir = (Path(args.output_dir) if args.output_dir
               else dashboard_dir() / _today().strftime("%Y-%m-%d"))
    out_dir.mkdir(parents=True, exist_ok=True)

    html_path = out_dir / "morning-dashboard.html"

    print("Collecting data...")

    crm = collect_crm_health()
    # Four buckets, four numbers.
    print(f"  CRM: {crm['total']} contacts ({len(crm['red'])} red, {len(crm['yellow'])} yellow, "
          f"{len(crm['green'])} green, {len(crm['gray'])} unreadable)")

    pipeline = collect_pipeline()
    print(f"  Pipeline: {pipeline['total_deals']} deals, {pipeline['total_investors']} investors, {pipeline['total_partnerships']} partnerships")

    calendar = collect_calendar()
    print(f"  Calendar: {len(calendar['meetings'])} meetings today")

    emails = collect_emails()
    print(f"  Email: {emails['count']} recent emails")

    strategy = collect_strategy()
    print(f"  Strategy: heading = {strategy['heading']}")

    metrics = collect_metrics()
    print(f"  Metrics: {metrics['headcount']} headcount, {metrics['modules_live']} modules live")

    freshness = collect_freshness()
    print(f"  Freshness: {freshness_summary(freshness)}")

    hiring = collect_hiring()
    print(f"  Hiring: {hiring['total']} open roles ({len(hiring['p1'])} P1, {len(hiring['p2'])} P2, {len(hiring['p3'])} P3), {len(hiring['urgent'])} urgent")

    content_cadence = collect_content_cadence()
    print(f"  Content: newsletter {content_cadence['newsletter_status']}, LinkedIn {content_cadence['linkedin_status']} ({content_cadence['linkedin_count_week']} this week)")

    assistant_tasks = collect_assistant_inbox()
    print(f"  assistant inbox: {assistant_tasks['active_total']} active tasks ({assistant_tasks['p1']} P1, {assistant_tasks['p2']} P2, {assistant_tasks['p3']} P3), {assistant_tasks['aging']} aging")

    capture_payoff = collect_capture_payoff()
    if capture_payoff.get("available"):
        promote_ready = capture_payoff.get("promote_ready")
        clusters = "unknown" if promote_ready is None else promote_ready
        line = (f"  Capture payoff: {capture_payoff['signals_week']} signals/7d, "
                f"{clusters} cluster(s) to promote")
        if capture_payoff.get("cadence_error"):
            line += f" [cadence unread: {capture_payoff['cadence_error']}]"
        print(line)

    print("\nGenerating HTML...")
    html_content = generate_html(crm, pipeline, calendar, emails, strategy, metrics, freshness,
                                 hiring, content_cadence, assistant_tasks, capture_payoff)
    html_path.write_text(html_content, encoding="utf-8")
    size = html_path.stat().st_size
    print(f"  Dashboard: {html_path}")
    print(f"  Size: {size:,} bytes")

    pdf_failed = False
    if args.pdf:
        print("\nGenerating PDF...")
        pdf_path = out_dir / "morning-dashboard.pdf"
        try:
            subprocess.run(
                [str(venv_python()), str(HTML_TO_PDF_SCRIPT), str(html_path), str(pdf_path)],
                check=True, timeout=60
            )
        except Exception as e:
            print(f"  PDF generation failed: {e}", file=sys.stderr)
            pdf_failed = True

    # A requested output that was not produced is a failed run.
    if pdf_failed:
        print("\nDone, WITHOUT the requested PDF.")
        return 1

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
