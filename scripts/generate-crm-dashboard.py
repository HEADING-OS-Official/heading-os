#!/usr/bin/env python3
"""
CRM Command Center Dashboard Generator

Renders the operator's own CRM contact cards, with the health `scan_contacts`
computes for them, plus the pipeline correlation into a single-page HTML
dashboard. Self-contained (inline CSS, base64 logo, no external dependencies
beyond Google Fonts).

The radar comes from the cards themselves, through the same `scan_contacts`
that `crm-health.py` uses.

Usage:
    python scripts/generate-crm-dashboard.py                  # HTML only
    python scripts/generate-crm-dashboard.py --pdf            # HTML + PDF
    python scripts/generate-crm-dashboard.py --json           # raw data as JSON
    python scripts/generate-crm-dashboard.py --output-dir DIR # custom output dir

Tests: none (M050).
"""

import argparse
import html
import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.utils.brand_assets import brand_asset_path
from scripts.utils.html_templates import load_template
from scripts.utils.image import load_logo_base64
from scripts.utils.markdown import parse_md_table
from scripts.utils.crm import (
    crm_config_path,
    parse_config,
    pipeline_path,
    scan_contacts,
)
from scripts.utils.data_path import data_path
from scripts.utils.venv_guard import venv_python
from scripts.utils.workspace import make_run_clock
from scripts.utils.colors import GREEN, YELLOW, RED, CYAN, BOLD, RESET

# ============================================================
# Paths
# ============================================================
SCRIPT_DIR = Path(__file__).resolve().parent

HTML_TO_PDF_SCRIPT = SCRIPT_DIR / "html-to-pdf.py"


def dashboard_dir() -> Path:
    """Where the rendered CRM command centres go: the `crm-dashboard` registry name.

    The registry names the directory, not the dated leaf below it. Resolved per
    call; raises `UnknownDataPath` on a clone without the name.
    """
    return data_path('crm-dashboard')


def pipeline_file():
    """The `pipeline` record, through the shared CRM resolver."""
    return pipeline_path()


def logo_path():
    """The white mark, asked for by key rather than by filename.

    A datastore filename is itself private, so the key comes from the manifest,
    as in `scripts/generate-dashboard.py`.
    """
    return brand_asset_path("logo_on_dark")


# This run's single wall-clock reading, taken at first use rather than at
# import, and memoised: the date and time are read in nine places below, among them the output directory name and the page title, and one reading keeps a run that
# straddles midnight consistent. Why both: `make_run_clock` in
# scripts/utils/workspace.py.
_clock = make_run_clock()


def _today() -> date:
    """This run's local date. See `_clock`."""
    return _clock().date()


def _now() -> datetime:
    """This run's local timestamp. See `_clock`."""
    return _clock()


# ============================================================
# Utilities
# ============================================================
def esc(text):
    """HTML-escape a string, returning empty string for None/empty."""
    if not text:
        return ""
    return html.escape(str(text))


def read_file(path):
    """Read a file and return its content, or empty string if missing."""
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


# ============================================================
# Health classification
# ============================================================
HEALTH_CLASSES = ("RED", "YELLOW", "GREEN", "GRAY")
BADGE_CLASS = {"RED": "badge-red", "YELLOW": "badge-yellow",
               "GREEN": "badge-green", "GRAY": "badge-gray"}

_HEALTH_WARNED: set[tuple[str, str]] = set()


def normalise_health(raw, name="?"):
    """The ONE health class a contact carries, whichever surface asks.

    Resolved once, at the collector, so the cards, the table and the badges
    cannot treat one unrecognised value three ways.

    An unrecognised value resolves GRAY and is NAMED on stderr, so resolving it
    never becomes hiding it. Named once per offending contact and value, for
    the life of the process: the resolution runs at collection AND at every
    render and export that asks, so an undeduped print put one warning per
    caller on the console for a single bad cell.
    """
    h = str(raw or "").strip().upper()
    if h in HEALTH_CLASSES:
        return h
    key = (str(name), str(raw))
    if key not in _HEALTH_WARNED:
        _HEALTH_WARNED.add(key)
        print(f"  Warning: contact {name!r} has health "
              f"{raw!r}; counted as GRAY", file=sys.stderr)
    return "GRAY"


def _health_counts(contacts):
    """Contacts per health card. Every contact lands in exactly one.

    So the four cards always sum to the header total.
    """
    counts = dict.fromkeys(HEALTH_CLASSES, 0)
    for c in contacts:
        counts[normalise_health(c.get("health"), c.get("name", "?"))] += 1
    return counts


# ============================================================
# Data Collectors
# ============================================================
def collect_radar():
    """Every contact card with its health, from `scan_contacts`.

    The same scan and the same cadence table `crm-health.py` reads, so this
    page and the terminal radar cannot disagree about who is red. `today` is
    this run's single clock reading, not the helper's own, so the page date
    and every `days_since` on it come from one instant.
    """
    config = parse_config(crm_config_path())
    raw, _tribe_warnings, _dangling_refs, _stages, _aliases = scan_contacts(
        config, today=_today())
    contacts = []
    for c in raw:
        name = str(c.get("name", "")).strip()
        contacts.append({
            "name": name,
            "company": str(c.get("company", "")).strip(),
            "type": str(c.get("type", "")).strip(),
            "last_touch": str(c.get("last_touch", "")).strip(),
            "days_since": c.get("days_since"),
            "cadence": c.get("cadence"),
            "health": normalise_health(c.get("health"), name or "?"),
        })
    return contacts


def collect_pipeline_companies():
    """Parse pipeline.md to extract company names for correlation."""
    pipeline = pipeline_file()
    content = read_file(pipeline)
    if not content:
        return []
    deals = parse_md_table(content, r"##\s*Active Deals", source=str(pipeline))
    companies = []
    for d in deals:
        company = d.get("Company", "").strip()
        if company:
            companies.append({
                "company": company,
                "country": d.get("Country", "").strip(),
                "stage": d.get("Stage", "").strip(),
                "value": d.get("Est. Value", "").strip(),
                "owner": d.get("Owner", "").strip(),
            })
    return companies


def correlate_pipeline_crm(radar_contacts, pipeline_companies):
    """Match CRM contacts by company against pipeline deals, with a strength.

    A substring match is not proof of the same company, so it is labelled.

    Equality is the only rule that carries no doubt, and on its own it is too
    strict for pipeline text: a deal row spelling a legal suffix the CRM card
    omits is a real correlation worth surfacing. So the weaker matches survive
    and are labelled `partial` instead of shipped as equals, and
    `build_pipeline_correlation` renders them visibly weaker.
    """
    matches = []
    seen_companies = set()
    for deal in pipeline_companies:
        deal_company_lower = deal["company"].strip().lower()
        for contact in radar_contacts:
            contact_company_lower = contact["company"].strip().lower()
            if not contact_company_lower or not deal_company_lower:
                continue
            if contact_company_lower == deal_company_lower:
                strength = "exact"
            elif (contact_company_lower in deal_company_lower
                    or deal_company_lower in contact_company_lower):
                strength = "partial"
            else:
                continue
            key = (deal["company"], contact["name"])
            if key not in seen_companies:
                seen_companies.add(key)
                matches.append({
                    "deal_company": deal["company"],
                    "contact_name": contact["name"],
                    "contact_company": contact["company"],
                    "stage": deal["stage"],
                    "value": deal["value"],
                    "crm-health": contact["health"],
                    "deal_owner": deal["owner"],
                    "match": strength,
                })
    return matches


# ============================================================
# CSS
# ============================================================
def build_css():
    return load_template("crm-dashboard.css")


# ============================================================
# Rendering / HTML Section Builders
# ============================================================
def build_header(logo_b64, total_contacts):
    date_long = _now().strftime("%A, %B %d, %Y")
    # The zone's own abbreviation; the parenthetical is dropped when `tzname()` has none.
    time_str = _now().strftime("%H:%M")
    zone = _now().tzname()
    time_html = f"{esc(time_str)} ({esc(zone)})" if zone else esc(time_str)
    logo_html = ""
    if logo_b64:
        logo_html = f'<img class="header-logo" src="{logo_b64}" alt="Logo"/>'
    return f"""
<div class="header">
  <div class="header-left">
    {logo_html}
    <span class="header-title">CRM Command Center</span>
  </div>
  <div class="header-right">
    <span class="header-badge badge-muted">{total_contacts} Contacts</span>
    <div class="header-date">{esc(date_long)}<br/>{time_html}</div>
  </div>
</div>
"""


def build_health_summary(contacts):
    counts = _health_counts(contacts)
    return f"""
<div class="health-row">
  <div class="health-card red">
    <div class="health-num">{counts['RED']}</div>
    <div class="health-label">Red - Overdue</div>
  </div>
  <div class="health-card yellow">
    <div class="health-num">{counts['YELLOW']}</div>
    <div class="health-label">Yellow - Due Soon</div>
  </div>
  <div class="health-card green">
    <div class="health-num">{counts['GREEN']}</div>
    <div class="health-label">Green - Healthy</div>
  </div>
  <div class="health-card gray">
    <div class="health-num">{counts['GRAY']}</div>
    <div class="health-label">Gray - No Cadence</div>
  </div>
</div>
"""


def build_radar_table(contacts, limit=50):
    if not contacts:
        return """
<div class="section">
  <div class="section-title">Contact Radar</div>
  <div class="empty">No radar data available.</div>
</div>
"""
    # Sort: RED first, then YELLOW, GREEN, GRAY, through `normalise_health` like the cards.
    order = {"RED": 0, "YELLOW": 1, "GREEN": 2, "GRAY": 3}
    sorted_contacts = sorted(
        contacts,
        key=lambda c: (order[normalise_health(c["health"], c.get("name", "?"))],
                       -(c["days_since"] or 0)))

    total = len(sorted_contacts)
    display = sorted_contacts[:limit]
    rows_html = ""
    for c in display:
        health = normalise_health(c["health"], c.get("name", "?"))
        days_str = str(c["days_since"]) if c["days_since"] is not None else "-"
        rows_html += f"""
<tr>
  <td>{esc(c['name'])}</td>
  <td>{esc(c['company'])}</td>
  <td>{esc(c['type'])}</td>
  <td style="text-align:right;">{esc(days_str)}</td>
  <td><span class="badge {BADGE_CLASS[health]}">{esc(health)}</span></td>
</tr>"""

    truncate_html = ""
    remaining = total - limit
    if remaining > 0:
        truncate_html = f'<div class="truncate-note">...and {remaining} more contacts</div>'

    return f"""
<div class="section">
  <div class="section-title">Contact Radar</div>
  <table class="data-table">
    <thead><tr>
      <th>Contact</th><th>Company</th><th>Type</th>
      <th style="text-align:right;">Days Since</th><th>Health</th>
    </tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
  {truncate_html}
</div>
"""


def build_type_distribution(contacts):
    if not contacts:
        return """
<div class="section">
  <div class="section-title">Type Distribution</div>
  <div class="empty">No data available.</div>
</div>
"""
    type_counts = {}
    for c in contacts:
        t = c["type"] if c["type"] else "unknown"
        type_counts[t] = type_counts.get(t, 0) + 1

    # Sort by count descending
    sorted_types = sorted(type_counts.items(), key=lambda x: -x[1])
    max_count = sorted_types[0][1] if sorted_types else 1

    bars_html = ""
    for t, count in sorted_types:
        pct = (count / max_count) * 100 if max_count > 0 else 0
        bars_html += f"""
<div class="bar-container">
  <div class="bar-label">{esc(t)}</div>
  <div class="bar-track">
    <div class="bar-fill" style="width:{pct:.0f}%;">
      <span class="bar-value">{count}</span>
    </div>
  </div>
</div>"""

    return f"""
<div class="section">
  <div class="section-title">Type Distribution</div>
  {bars_html}
</div>
"""


def build_top_overdue(contacts, limit=15):
    # Filter to only contacts with days_since and RED/YELLOW health
    overdue = [
        c for c in contacts
        if c["days_since"] is not None and c["health"] in ("RED", "YELLOW")
    ]
    overdue.sort(key=lambda c: -(c["days_since"] or 0))
    display = overdue[:limit]

    if not display:
        # `limit`, not a hardcoded 15, so both headings agree.
        return f"""
<div class="section">
  <div class="section-title">Top {limit} Overdue</div>
  <div class="empty">No overdue contacts.</div>
</div>
"""
    rows_html = ""
    for c in display:
        rows_html += f"""
<tr>
  <td>{esc(c['name'])}</td>
  <td>{esc(c['company'])}</td>
  <td style="text-align:right;color:var(--red);font-weight:600;">{c['days_since']}d</td>
</tr>"""

    return f"""
<div class="section">
  <div class="section-title">Top {min(limit, len(display))} Overdue</div>
  <table class="data-table">
    <thead><tr>
      <th>Contact</th><th>Company</th>
      <th style="text-align:right;">Days Overdue</th>
    </tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
</div>
"""


def build_pipeline_correlation(correlations):
    if not correlations:
        return """
<div class="section">
  <div class="section-title">Pipeline Correlation</div>
  <div class="empty">No matching companies found between CRM and pipeline.</div>
</div>
"""
    rows_html = ""
    partials = 0
    for m in correlations:
        health = normalise_health(m.get("crm-health"), m.get("contact_name", "?"))
        # Anything not labelled `exact` is the weaker one, including a row that
        # carries no label at all. An unlabelled correlation is not evidence
        # that the two company names were equal.
        if m.get("match") == "exact":
            match_html = "exact"
        else:
            partials += 1
            match_html = '<span class="badge badge-gray">partial</span>'
        rows_html += f"""
<tr>
  <td>{esc(m['deal_company'])}</td>
  <td>{esc(m['contact_name'])}</td>
  <td>{esc(m['stage'])}</td>
  <td>{esc(m['value'])}</td>
  <td><span class="badge {BADGE_CLASS[health]}">{esc(health)}</span></td>
  <td>{match_html}</td>
</tr>"""

    note_html = ""
    if partials:
        note_html = (
            '<div class="truncate-note">'
            f'{partials} of these rows matched on a partial company name, not '
            'an identical one. Read the stage and value beside them as a lead, '
            'not as a confirmed correlation.</div>')

    return f"""
<div class="section">
  <div class="section-title">Pipeline Correlation</div>
  <table class="data-table">
    <thead><tr>
      <th>Deal / Company</th><th>CRM Contact</th><th>Stage</th>
      <th>Est. Value</th><th>CRM Health</th><th>Match</th>
    </tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
  {note_html}
</div>
"""


def build_footer():
    return f"""
<div class="footer">
  <div class="footer-text">CRM Command Center | Generated {esc(_now().strftime("%Y-%m-%d %H:%M"))}</div>
  <div class="footer-class">Internal - CEO Eyes Only</div>
</div>
"""


# ============================================================
# Full HTML Assembly
# ============================================================
def generate_html(radar_contacts, pipeline_correlations):
    css = build_css()
    logo_b64 = load_logo_base64(logo_path())

    total_contacts = len(radar_contacts)

    sections = [
        build_header(logo_b64, total_contacts),
        build_health_summary(radar_contacts),
        build_radar_table(radar_contacts, limit=50),
        build_type_distribution(radar_contacts),
        build_top_overdue(radar_contacts, limit=15),
        build_pipeline_correlation(pipeline_correlations),
        build_footer(),
    ]

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>CRM Command Center - {esc(_today().strftime("%Y-%m-%d"))}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet"/>
<style>{css}</style>
</head>
<body>
<div class="page">
{"".join(sections)}
</div>
</body>
</html>"""


# ============================================================
# Output / JSON Export
# ============================================================
def build_json_export(radar_contacts, pipeline_correlations):
    counts = _health_counts(radar_contacts)

    return {
        "generated": _now().isoformat(),
        "total_contacts": len(radar_contacts),
        "health_summary": counts,
        "contacts": radar_contacts,
        "pipeline_correlations": pipeline_correlations,
    }


# ============================================================
# CLI / Main
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="CRM Command Center Dashboard")
    parser.add_argument("--output-dir", help="Custom output directory")
    parser.add_argument("--pdf", action="store_true", help="Also generate PDF via html-to-pdf.py")
    parser.add_argument("--json", action="store_true", help="Output raw data as JSON")
    args = parser.parse_args()

    # Determine output directory
    out_dir = (Path(args.output_dir) if args.output_dir
               else dashboard_dir() / _today().strftime("%Y-%m-%d"))
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"{BOLD}CRM Command Center{RESET}")
    print(f"{'=' * 40}")

    # Step 1: Collect all data sources
    print(f"\n{CYAN}Collecting data...{RESET}")

    radar_contacts = collect_radar()
    # Through `_health_counts`, like the HTML and the JSON export, so the console agrees with the page.
    health_counts = _health_counts(radar_contacts)
    print(f"  Radar: {len(radar_contacts)} contacts "
          f"({health_counts['RED']} red, {health_counts['YELLOW']} yellow, "
          f"{health_counts['GREEN']} green, {health_counts['GRAY']} gray)")

    pipeline_companies = collect_pipeline_companies()
    print(f"  Pipeline: {len(pipeline_companies)} active deals")

    correlations = correlate_pipeline_crm(radar_contacts, pipeline_companies)
    print(f"  Correlations: {len(correlations)} CRM-pipeline matches")

    # Step 2: Generate output
    if args.json and args.pdf:
        # The PDF is rendered from the HTML, so --json --pdf says the PDF is skipped.
        print(f"  {YELLOW}Warning: --pdf is ignored with --json "
              f"(the PDF is rendered from the HTML output){RESET}",
              file=sys.stderr)
    if args.json:
        json_path = out_dir / "crm-command-center.json"
        data = build_json_export(radar_contacts, correlations)
        json_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        print(f"\n{GREEN}JSON: {json_path}{RESET}")
        print(f"  Size: {json_path.stat().st_size:,} bytes")
    else:
        print(f"\n{CYAN}Generating HTML...{RESET}")
        html_content = generate_html(radar_contacts, correlations)
        html_path = out_dir / "crm-command-center.html"
        html_path.write_text(html_content, encoding="utf-8")
        size = html_path.stat().st_size
        print(f"  {GREEN}Dashboard: {html_path}{RESET}")
        print(f"  Size: {size:,} bytes")

        if args.pdf:
            print(f"\n{CYAN}Generating PDF...{RESET}")
            pdf_path = out_dir / "crm-command-center.pdf"
            try:
                subprocess.run(
                    [str(venv_python()), str(HTML_TO_PDF_SCRIPT), str(html_path), str(pdf_path)],
                    check=True, timeout=60
                )
                print(f"  {GREEN}PDF: {pdf_path}{RESET}")
            except Exception as e:
                print(f"  {RED}PDF generation failed: {e}{RESET}", file=sys.stderr)

    print(f"\n{GREEN}Done.{RESET}")


if __name__ == "__main__":
    main()
