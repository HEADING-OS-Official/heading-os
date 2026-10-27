#!/usr/bin/env python3
"""
Intelligence Briefing - HTML Newsletter Generator (V3)

Generates a branded HTML newsletter, plus a single-page PDF, from a structured
JSON issue: masthead, hero, indicator bar, CSS-only data visualizations and a
newspaper-style layout.

Usage:
    python scripts/generate-newsletter-html.py <input.json> [--output-dir DIR] [--images section=path ...]

If --output-dir is omitted, saves to YYYY-MM-DD/ under data_path('newsletters').
It renders files and nothing else: it never sends, mails or publishes an issue.

The brand comes from the data repository, never from this file. Both files are
read from the generator templates of the registry name `brand-kit` (the
directory `scripts.utils.html_templates.templates_dir()` answers):

    newsletter.css         the stylesheet, inlined whole
    newsletter-brand.json  the brand settings, every field optional:
        name              masthead name when there is no logo, page title
        subtitle          line under the masthead logo or name
        mark              short code in the top bar; a "/" is set with thin spaces
        tagline           footer line
        website           footer link, https only
        copyright_holder  footer copyright holder (default: name)
        regions           default region list for an issue that names none
        fonts_css_url     stylesheet URL for web fonts, https only
        colors            {"<css-variable>": "<colour>"}, laid over :root
        logo              logo image, a path relative to the brand-kit directory
        sections          {"<section key>": {"kicker": ..., "title": ...}}
        region_aliases    {"<alias key>": "<region key>"}: one region, two spellings
        image_palette     palette words for illustrations (read by the skill)

With no stylesheet the issue renders in a plain neutral style, and with no
settings file under neutral defaults; each case is said on stderr, never
silently.

Tests: none (M050).
"""

import json
import sys
import base64
import html
import re
import argparse
from pathlib import Path
from datetime import date, datetime

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.utils.html_templates import load_template, templates_dir
from scripts.utils.user_agent import user_agent
from scripts.utils.html_text import strip_html
from scripts.utils.image import load_logo_base64
from scripts.utils.markdown import frontmatter_date
from scripts.utils.sanitize_text import word_count
from scripts.utils.data_path import DataPathError, data_path
from scripts.utils.workspace import get_default_tz


# ============================================================
# Paths
# ============================================================
def newsletters_dir() -> Path:
    """The root the dated briefing directories are published under.

    The SAME address `scripts/generate-dashboard.py::newsletters_dir` reads to
    date the cadence panel, so the two must name one registry entry
    (`newsletters`); an unnamed entry raises `UnknownDataPath`.
    """
    return data_path('newsletters')


# ============================================================
# Brand settings
# ============================================================
BRAND_SETTINGS_FILE = "newsletter-brand.json"

DEFAULT_BRAND = {
    "name": "Intelligence Briefing",
    "subtitle": "",
    "mark": "",
    "tagline": "",
    "website": "",
    "copyright_holder": "",
    "regions": [],
    "fonts_css_url": "",
    "colors": {},
    "logo": "",
    "sections": {},
    "region_aliases": {},
}

_CSS_VAR_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_CSS_COLOUR_RE = re.compile(r"^(#[0-9a-fA-F]{3,8}|[a-zA-Z]+|rgba?\([0-9.,%\s]+\))$")


def load_brand() -> dict:
    """The brand settings, laid over neutral defaults.

    A missing file is the normal state of a fresh data repository, so it is
    said once on stderr and the defaults render. A file that is there but is
    not a JSON object is a fault in the operator's own configuration, and
    refuses rather than rendering an issue under somebody else's defaults.
    """
    brand = dict(DEFAULT_BRAND)
    try:
        path = templates_dir() / BRAND_SETTINGS_FILE
    except DataPathError as exc:
        print(f"Note: no data repository ({exc}); rendering with neutral brand defaults.",
              file=sys.stderr)
        return brand
    if not path.exists():
        print(f"Note: no brand settings at {path}; rendering with neutral brand defaults. "
              "The fields are documented at the top of scripts/generate-newsletter-html.py.",
              file=sys.stderr)
        return brand
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Error: brand settings {path} could not be read: {exc}") from exc
    if not isinstance(settings, dict):
        raise SystemExit(f"Error: brand settings {path} must hold a JSON object")
    brand.update(settings)
    return brand


def brand_text(brand: dict, key: str) -> str:
    """A brand field as text; anything that is not a string reads as empty."""
    value = brand.get(key)
    return value if isinstance(value, str) else ""


def logo_path(brand: dict) -> Path | None:
    """The masthead logo named by the brand settings, or None when there is none.

    The `logo` field is relative to the brand-kit directory and is held inside
    it: a value that resolves outside (`..`, an absolute path) is refused with
    a warning and the issue renders without a logo.
    """
    rel = brand_text(brand, "logo")
    if not rel:
        return None
    try:
        base = data_path("brand-kit").resolve()
    except DataPathError:
        return None
    candidate = (base / rel).resolve()
    if not candidate.is_relative_to(base):
        print(f"Warning: brand logo {rel!r} is outside the brand kit; rendering without it",
              file=sys.stderr)
        return None
    if not candidate.is_file():
        print(f"Warning: brand logo not found, rendering without it: {candidate}",
              file=sys.stderr)
        return None
    return candidate


def brand_colours_css(brand: dict) -> str:
    """The `colors` field as a `:root` block; an unsafe name or value is skipped, said."""
    colours = brand.get("colors")
    if not isinstance(colours, dict) or not colours:
        return ""
    decls = []
    for name, value in colours.items():
        if (isinstance(name, str) and isinstance(value, str)
                and _CSS_VAR_RE.match(name) and _CSS_COLOUR_RE.match(value.strip())):
            decls.append(f"  --{name}: {value.strip()};")
        else:
            print(f"Warning: brand colour {name!r}: {value!r} is not a CSS variable and colour; skipped",
                  file=sys.stderr)
    return ":root {\n" + "\n".join(decls) + "\n}\n" if decls else ""


def section_label(brand: dict, key: str, kicker: str, title: str) -> tuple[str, str]:
    """A section's kicker and title, as the brand settings rename them."""
    override = (brand.get("sections") or {}).get(key) if isinstance(brand.get("sections"), dict) else None
    if not isinstance(override, dict):
        return kicker, title
    k, t = override.get("kicker"), override.get("title")
    return (k if isinstance(k, str) else kicker), (t if isinstance(t, str) else title)


# Every section name `--images` will render. One name today, and the set is
# named rather than inlined so the CLI check and the consumer in
# `generate_newsletter` cannot drift apart the way a hardcoded list would.
IMAGE_SECTIONS = frozenset({"sea_state"})


# ============================================================
# Utilities
# ============================================================
def embed_image(file_path):
    """Read an image file and return a base64 data URI string.

    A missing file yields `""`, which every caller renders as no image at all.
    That is the right OUTPUT (a briefing with a missing figure still ships),
    but it was also silent, so a mistyped `--images` path produced an issue
    with a hole in it and no line anywhere saying why.
    """
    path = Path(file_path)
    if not path.exists():
        print(f"Warning: image not found, rendering without it: {file_path}",
              file=sys.stderr)
        return ""
    suffix = path.suffix.lower().lstrip(".")
    mime_map = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}
    mime = mime_map.get(suffix, "image/png")
    data = path.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def esc(text):
    """HTML-escape text content. `None` and `""` render as nothing; 0 does not.

    Zero is a finding in a briefing, so only `None` and `""` are treated as empty.
    """
    if text is None or text == "":
        return ""
    return html.escape(str(text))


SAFE_URL_SCHEMES = ("http://", "https://", "mailto:")


def safe_url(url, fallback="#"):
    """An href value that cannot execute and cannot leave its attribute.

    Further Reading carries scraped external links, so a `javascript:` url or one
    with a quote must never reach `href` raw.

    Anything that is not http, https or mailto becomes `fallback`. A relative
    path is refused too: this document is mailed and rendered to PDF, where a
    relative link resolves against nothing useful.
    """
    if not url:
        return fallback
    candidate = str(url).strip()
    if not url_scheme_ok(candidate):
        return fallback
    return html.escape(candidate, quote=True)


def url_scheme_ok(url) -> bool:
    """True when the url starts with a scheme that cannot execute.

    Split out for the markdown link renderer, whose input has ALREADY been
    html-escaped. Escaping it a second time would turn `&` into `&amp;amp;`
    and corrupt every query string, so that path checks the scheme and leaves
    the text alone. A scheme name contains nothing html.escape touches, so
    testing the escaped string is the same test.
    """
    return bool(url) and str(url).strip().lower().startswith(SAFE_URL_SCHEMES)


def nl2br(text):
    """Convert newlines in a string to <br/> tags (for region names, titles)."""
    if not text:
        return ""
    return esc(text).replace("\n", "<br/>")


def markdown_to_html(text):
    """Minimal markdown-to-HTML for newsletter body content.

    Supports: paragraphs, **bold**, [links](url), bullet lists (- item).

    Coerced with `str()` here, once for every caller, because the input JSON's
    types are not ours.
    """
    if not text:
        return ""

    lines = str(text).strip().split("\n")
    paragraphs = []
    current = []

    for line in lines:
        stripped = line.strip()
        if stripped == "":
            if current:
                paragraphs.append(" ".join(current))
                current = []
        elif stripped.startswith("- "):
            # A bullet is its own paragraph, so consecutive items need no blank line between them.
            if current:
                paragraphs.append(" ".join(current))
                current = []
            paragraphs.append(stripped)
        else:
            current.append(stripped)
    if current:
        paragraphs.append(" ".join(current))

    result = []
    for para in paragraphs:
        p = html.escape(para)
        # Bold
        p = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", p)
        # Links: the scheme is checked (no `javascript:`), and the URL group
        # balances one level of parentheses so Wikipedia-style URLs survive.
        p = re.sub(
            r"\[(.+?)\]\(((?:[^()]|\([^()]*\))*)\)",
            lambda m: (f'<a href="{m.group(2)}" target="_blank">{m.group(1)}</a>'
                       if url_scheme_ok(m.group(2)) else m.group(1)),
            p,
        )
        # Bullet items
        if p.startswith("- "):
            p = f"<li>{p[2:]}</li>"
        else:
            p = f"<p>{p}</p>"
        result.append(p)

    # Wrap consecutive <li> in <ul>
    final = []
    in_list = False
    for item in result:
        if item.startswith("<li"):
            if not in_list:
                final.append("<ul>")
                in_list = True
            final.append(item)
        else:
            if in_list:
                final.append("</ul>")
                in_list = False
            final.append(item)
    if in_list:
        final.append("</ul>")

    return "\n".join(final)


# ============================================================
# CSS
# ============================================================
# The style an issue renders in when the brand kit holds no stylesheet: plain,
# readable and deliberately unbranded. Every class the builders emit has a rule.
NEUTRAL_CSS = """
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
:root{--bg:#f2f2f0;--white:#ffffff;--ink:#1b1b1b;--ink60:#5a5a5a;--ink35:#9a9a9a;--ink12:#e2e2e2;
--accent:#2f5d8a;--accent-light:#eaf0f6;--green:#2e6b3f;--red:#a3322a;}
body{background:var(--bg);color:var(--ink);font-family:Georgia,'Times New Roman',serif;line-height:1.55;}
.page{max-width:700px;margin:0 auto;background:var(--white);border:1px solid var(--ink12);}
.topbar{background:var(--ink);color:#cfcfcf;padding:8px 28px;display:flex;justify-content:space-between;
font:9px/1.4 'Courier New',monospace;letter-spacing:1.5px;text-transform:uppercase;}
.topbar-left{display:flex;align-items:center;gap:8px;}
.pulse{width:6px;height:6px;border-radius:50%;background:var(--accent);}
.masthead{padding:24px 28px;border-bottom:2px solid var(--ink);}
.mast-row1{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;}
.logo-img{max-height:48px;max-width:220px;display:block;margin-bottom:6px;}
.brand-name{font:700 26px/1.1 Arial,Helvetica,sans-serif;letter-spacing:1px;text-transform:uppercase;}
.brand-sub,.mast-date,.threat-lbl,.sec-kicker,.ind-lbl,.mstat-lbl,.read-src,.img-cap,.ft-date,.pq-attr{
font:9px/1.5 'Courier New',monospace;letter-spacing:1px;text-transform:uppercase;color:var(--ink60);}
.mast-right{text-align:right;}.threat{margin-top:8px;}
.threat-val{display:inline-block;margin-left:6px;padding:2px 8px;background:var(--red);color:#fff;
font:700 11px Arial,sans-serif;letter-spacing:1px;}
.hero{position:relative;padding:32px 28px;background:var(--accent-light);border-bottom:1px solid var(--ink12);}
.hero-kicker{font:10px 'Courier New',monospace;letter-spacing:2px;text-transform:uppercase;color:var(--accent);}
.hero-title{font:700 40px/1.05 Arial,Helvetica,sans-serif;text-transform:uppercase;margin:10px 0;}
.accent{color:var(--accent);}.hero-deck{font-size:16px;font-style:italic;color:var(--ink60);}
.indicators{display:flex;border-bottom:1px solid var(--ink12);}
.ind{flex:1;padding:12px 10px;text-align:center;border-right:1px solid var(--ink12);}
.ind:last-child{border-right:none;}
.ind-val{display:block;font:700 18px Arial,sans-serif;}.ind-val.up{color:var(--green);}.ind-val.danger{color:var(--red);}
.content{padding:8px 28px 24px;}.section{padding:22px 0;border-bottom:1px solid var(--ink12);}
.section p{margin:0 0 10px;font-size:14.5px;}.section p.lede{font-size:16px;}.section ul{margin:0 0 10px 18px;}
.section a{color:var(--accent);}
.sec-header{display:flex;align-items:baseline;gap:12px;margin-bottom:12px;}
.sec-num{font:700 28px Arial,sans-serif;color:var(--ink35);}
.sec-title-block{display:flex;flex-direction:column;}.sec-title{font:700 20px Arial,sans-serif;text-transform:uppercase;}
.vis-banner{position:relative;min-height:110px;margin:6px 0 4px;background:var(--ink);color:#fff;overflow:hidden;}
.vis-banner img{width:100%;display:block;}
.vis-sea-text,.vis-cyber-text{position:relative;padding:18px;font-size:13px;}
.vis-sea-text strong,.vis-cyber-text strong{display:block;font:700 15px Arial,sans-serif;text-transform:uppercase;}
.vis-sea-img .vis-sea-text{position:absolute;left:0;bottom:0;background:rgba(0,0,0,.55);}
.cyber-badge{position:absolute;right:14px;top:14px;border:1px solid #fff;padding:6px 10px;text-align:center;
font:9px 'Courier New',monospace;text-transform:uppercase;}
.cyber-badge span{display:block;}
.img-cap{margin-bottom:12px;}
.big-stat{display:flex;gap:14px;align-items:center;margin-top:12px;padding:12px;background:var(--accent-light);}
.stat-num-block span{font:700 30px Arial,sans-serif;color:var(--accent);}
.stat-text-block strong{display:block;}
.region-row{display:flex;gap:16px;padding:12px 0;border-top:1px solid var(--ink12);}
.region-left{width:120px;flex-shrink:0;}.r-code{display:block;font:700 18px Arial,sans-serif;color:var(--accent);}
.r-name{display:block;font:9px 'Courier New',monospace;text-transform:uppercase;color:var(--ink60);}
.region-right{flex:1;}
.vis-markets{background:var(--white);border:1px solid var(--ink12);color:var(--ink);padding:12px;}
.chart-wrap{display:flex;align-items:flex-end;gap:4px;height:90px;}
.chart-bar{flex:1;background:var(--ink35);}.chart-bar.hi{background:var(--accent);}
.market-stats{display:flex;gap:18px;margin-top:10px;}.mstat-val{display:block;font:700 16px Arial,sans-serif;}
.mstat-val.up{color:var(--green);}.mstat-val.dn{color:var(--red);}.mstat-val.gold{color:var(--accent);}
.market-caption{margin-top:8px;font-size:12px;color:var(--ink60);}
.pullquote{position:relative;margin:16px 0;padding:14px 18px;border-left:3px solid var(--accent);}
.pq-bg-num{display:none;}.pq-text{font-size:18px;font-style:italic;}
.signal-row{display:flex;gap:12px;align-items:center;padding:8px 0;border-top:1px solid var(--ink12);}
.sig-idx{font:700 14px Arial,sans-serif;color:var(--ink35);}.sig-text{flex:1;font-size:14px;}
.sig-dot span{display:block;width:8px;height:8px;border-radius:50%;background:var(--accent);}
.read-item{display:flex;gap:12px;padding:8px 0;border-top:1px solid var(--ink12);}
.read-n{font:700 14px Arial,sans-serif;color:var(--ink35);}.read-a{color:var(--ink);font-weight:700;}
.footer{display:flex;justify-content:space-between;align-items:flex-end;padding:18px 28px;background:var(--ink);color:#cfcfcf;}
.footer a{color:#fff;}.logo-footer{max-height:28px;margin-bottom:6px;}
.ft-copy,.ft-tag{font-size:11px;}.ft-right{text-align:right;}.ft-iss{font:700 22px Arial,sans-serif;color:#fff;}
"""


def build_css(brand=None):
    """The <style> block content: the brand kit's stylesheet, then its colours.

    With no stylesheet in the brand kit, NEUTRAL_CSS renders instead and stderr
    says so, because an issue that silently changed its look is the failure
    `scripts/utils/html_templates.py` was written against.
    """
    try:
        css = load_template("newsletter.css")
    except (FileNotFoundError, DataPathError) as exc:
        print(f"Note: no brand stylesheet newsletter.css in the brand kit "
              f"({type(exc).__name__}); rendering in the neutral style.", file=sys.stderr)
        css = NEUTRAL_CSS
    colours = brand_colours_css(brand or {})
    return css + "\n" + colours if colours else css


# ============================================================
# Rendering / Builders
# ============================================================
def region_line(regions):
    """The regions joined for the top bar and masthead; none gives ""."""
    return " &middot; ".join(esc(r) for r in regions) if regions else ""


def build_top_bar(regions, brand=None):
    """Black bar with animated pulse dot, regions, classification."""
    region_str = region_line(regions)
    feed = "Live Intelligence Feed" + (f" &nbsp;&middot;&nbsp; {region_str}" if region_str else "")
    mark = esc(brand_text(brand or {}, "mark")).replace("/", "&thinsp;/&thinsp;")
    right = (f"{mark} &nbsp;&middot;&nbsp; " if mark else "") + "Unrestricted"
    return f"""
<div class="topbar">
  <div class="topbar-left"><div class="pulse"></div>{feed}</div>
  <div class="topbar-right">{right}</div>
</div>
"""


def build_masthead(logo_uri, display_date, issue_num, regions, threat_level, brand=None):
    """Two-column masthead with logo (or the brand name), date, issue, threat level."""
    brand = brand or {}
    region_str = region_line(regions)
    # `issue_number` is a document field: escaped here and in `build_footer`.
    issue_str = (f"Issue #{issue_num:03d}" if isinstance(issue_num, int)
                 else f"Issue #{esc(issue_num)}")

    if logo_uri:
        logo_html = f'<img src="{logo_uri}" alt="Logo" class="logo-img"/>'
    else:
        logo_html = f'<div class="brand-name">{esc(brand_text(brand, "name"))}</div>'
    subtitle = brand_text(brand, "subtitle")
    sub_html = f'<div class="brand-sub">{esc(subtitle)}</div>' if subtitle else ""

    return f"""
<div class="masthead">
  <div class="mast-row1">
    <div class="mast-brand">
      {logo_html}
      {sub_html}
    </div>
    <div class="mast-right">
      <div class="mast-meta">
        <div class="mast-date">
          {esc(display_date)}<br/>
          {issue_str}<br/>
          {region_str}
        </div>
      </div>
      <div class="threat">
        <span class="threat-lbl">Threat Level</span>
        <span class="threat-val">{esc(threat_level)}</span>
      </div>
    </div>
  </div>
</div>
"""


def build_hero(hero_data):
    """Hero section with large title, accent word, and deck text."""
    if not hero_data:
        return ""

    kicker = esc(hero_data.get("kicker", "Intelligence Briefing"))
    # The input JSON is an external document, so its types are coerced.
    title_raw = str(hero_data.get("title") or "")
    accent_word = str(hero_data.get("accent_word") or "")
    deck = esc(hero_data.get("deck", ""))

    # Build title with accent word highlighted and line breaks
    title_lines = title_raw.split("\n")
    title_parts = []
    for line in title_lines:
        line_esc = esc(line.strip())
        if accent_word and accent_word in line:
            line_esc = line_esc.replace(esc(accent_word), f'<span class="accent">{esc(accent_word)}</span>')
        title_parts.append(line_esc)
    title_html = "<br/>".join(title_parts)

    return f"""
  <div class="hero">
    <div class="hero-bg"></div>
    <div class="hero-content">
      <div class="hero-kicker">{kicker}</div>
      <div class="hero-title">{title_html}</div>
      <div class="hero-deck">{deck}</div>
    </div>
  </div>
"""


def build_indicators(items):
    """Indicator bar, one equal-width column per item in the list.

    `.indicators` is flex and `.ind` is `flex:1`, so any count lays out correctly.
    """
    if not items:
        return ""

    cols = []
    for item in items:
        # A scalar entry can only be the value.
        if not isinstance(item, dict):
            item = {"value": item}
        val = esc(item.get("value", ""))
        label = esc(item.get("label", ""))
        style = item.get("style", "neutral")
        css_class = "ind-val"
        if style == "up":
            css_class += " up"
        elif style == "danger":
            css_class += " danger"
        cols.append(f'    <div class="ind"><span class="{css_class}">{val}</span><span class="ind-lbl">{label}</span></div>')

    return f"""
  <div class="indicators">
{"".join(cols)}
  </div>
"""


def build_section_header(number, kicker, title):
    """Reusable section header with large number, kicker, and title."""
    num_str = f"{number:02d}" if isinstance(number, int) else str(number)
    return f"""
    <div class="sec-header">
      <span class="sec-num">{num_str}</span>
      <div class="sec-title-block">
        <span class="sec-kicker">{esc(kicker)}</span>
        <span class="sec-title">{esc(title)}</span>
      </div>
    </div>
"""


def build_sea_state(data, section_num=1, image_uri=None, brand=None):
    """Sea State section with radar banner or image, caption, and body."""
    if not data:
        return ""

    body = data if isinstance(data, str) else data.get("body", "")
    banner_title = data.get("banner_title", "") if isinstance(data, dict) else ""
    banner_detail = data.get("banner_detail", "") if isinstance(data, dict) else ""
    caption = data.get("caption", "") if isinstance(data, dict) else ""

    header = build_section_header(section_num, *section_label(brand or {}, "sea_state", "Kinetic Conflict", "Sea State"))

    # Banner: use image if provided, otherwise CSS radar
    if image_uri:
        banner = f"""
    <div class="vis-banner vis-sea-img">
      <img src="{image_uri}" alt="Sea State"/>
      <div class="vis-sea-text">
        <strong>{esc(banner_title)}</strong>
        {esc(banner_detail)}
      </div>
    </div>
"""
    else:
        banner = f"""
    <div class="vis-banner vis-sea">
      <div class="vis-sea-inner"></div>
      <div class="vis-sea-radar"></div>
      <div class="vis-sea-text">
        <strong>{esc(banner_title)}</strong>
        {esc(banner_detail)}
      </div>
    </div>
"""

    caption_html = f'    <div class="img-cap">{esc(caption)}</div>\n' if caption else ""

    # First paragraph gets .lede class
    body_html = markdown_to_html(body)
    body_html = body_html.replace("<p>", '<p class="lede">', 1)

    return f"""
  <div class="section">
{header}
{banner}
{caption_html}
    {body_html}
  </div>
"""


def build_cyber_front(data, section_num=2, brand=None):
    """Cyber Front section with scanline banner, APT badge, big stat, and body."""
    if not data:
        return ""

    body = data if isinstance(data, str) else data.get("body", "")
    banner_title = data.get("banner_title", "") if isinstance(data, dict) else ""
    banner_detail = data.get("banner_detail", "") if isinstance(data, dict) else ""
    caption = data.get("caption", "") if isinstance(data, dict) else ""
    badge = data.get("badge", {}) if isinstance(data, dict) else {}
    big_stat = data.get("big_stat", {}) if isinstance(data, dict) else {}

    header = build_section_header(section_num, *section_label(brand or {}, "cyber_front", "Cyber Operations", "The Cyber Front"))

    # APT badge
    badge_html = ""
    if badge:
        badge_top = esc(badge.get("top", ""))
        badge_name = esc(badge.get("name", ""))
        badge_bottom = esc(badge.get("bottom", ""))
        badge_html = f"""
      <div class="cyber-badge">
        <span>{badge_top}</span>
        <span class="big">{badge_name}</span>
        <span>{badge_bottom}</span>
      </div>
"""

    banner = f"""
    <div class="vis-banner vis-cyber">
      <div class="vis-cyber-inner">
        <div class="cyber-dots"></div>
        <div class="cyber-line"></div>
        <div class="cyber-line"></div>
        <div class="cyber-line"></div>
        <div class="cyber-line"></div>
        <div class="cyber-line"></div>
      </div>
{badge_html}
      <div class="vis-cyber-text">
        <strong>{esc(banner_title)}</strong>
        {esc(banner_detail)}
      </div>
    </div>
"""

    caption_html = f'    <div class="img-cap">{esc(caption)}</div>\n' if caption else ""

    body_html = markdown_to_html(body)

    # Big stat callout
    stat_html = ""
    if big_stat:
        stat_val = esc(big_stat.get("value", ""))
        stat_title = esc(big_stat.get("title", ""))
        stat_desc = esc(big_stat.get("description", ""))
        stat_html = f"""
    <div class="big-stat">
      <div class="stat-num-block"><span>{stat_val}</span></div>
      <div class="stat-text-block">
        <strong>{stat_title}</strong>
        <p>{stat_desc}</p>
      </div>
    </div>
"""

    return f"""
  <div class="section">
{header}
{banner}
{caption_html}
    {body_html}
{stat_html}
  </div>
"""


def build_navigation_chart(data, section_num=3, brand=None):
    """Navigation Chart section with region grid."""
    if not data:
        return ""

    header = build_section_header(section_num, *section_label(brand or {}, "navigation_chart", "Regional Intelligence", "Navigation Chart"))

    # A plain string is read as one unlabelled region body, like the sibling builders.
    if not isinstance(data, dict):
        data = {"": data}

    rows = []
    # Two keys for one region (the brand's `region_aliases`, alias -> key) used
    # to render that region twice. Document order; the first one present wins.
    aliases = (brand or {}).get("region_aliases")
    aliases = aliases if isinstance(aliases, dict) else {}
    seen_regions = set()
    for key in list(data):
        region = data.get(key)
        if not region:
            continue
        canonical = aliases.get(key, key)
        if canonical in seen_regions:
            continue
        seen_regions.add(canonical)
        if isinstance(region, str):
            # Escaped like the dict branch below: `.upper()` is no defence against markup in a key.
            code = esc(key.upper())
            name = nl2br(key.upper())
            body = region
        else:
            code = esc(region.get("code", key.upper()))
            name = nl2br(region.get("name", key.upper()))
            body = region.get("body", "")

        body_html = markdown_to_html(body)

        rows.append(f"""
      <div class="region-row">
        <div class="region-left">
          <span class="r-code">{code}</span>
          <span class="r-name">{name}</span>
        </div>
        <div class="region-right">
          {body_html}
        </div>
      </div>
""")

    return f"""
  <div class="section">
{header}
    <div class="region-table">
{"".join(rows)}
    </div>
  </div>
"""


def build_market_depth(data, section_num=4, brand=None):
    """Market Depth section with bar chart, stats overlay, body, and pullquote."""
    if not data:
        return ""

    body = data if isinstance(data, str) else data.get("body", "")
    bars = data.get("bars", []) if isinstance(data, dict) else []
    stats = data.get("stats", []) if isinstance(data, dict) else []
    caption = data.get("caption", "") if isinstance(data, dict) else ""
    pullquote = data.get("pullquote", {}) if isinstance(data, dict) else {}
    market_caption_text = data.get("market_caption", "") if isinstance(data, dict) else ""

    header = build_section_header(section_num, *section_label(brand or {}, "market_depth", "Capital Markets", "Market Depth"))

    # Bar chart, stats overlay and market caption: the stats render even with no bars.
    bar_html = ""
    if bars or stats or market_caption_text:
        bar_items = []
        for val in bars:
            # A bar is a percentage or it is not drawn.
            try:
                pct = float(val)
            except (TypeError, ValueError):
                print(f"Warning: market_depth bar {val!r} is not a number; skipped",
                      file=sys.stderr)
                continue
            pct = max(0.0, min(100.0, pct))
            hi_class = " hi" if pct > 60 else ""
            bar_items.append(f'        <div class="chart-bar{hi_class}" '
                             f'style="height:{pct:g}%"></div>')
        bars_joined = "\n".join(bar_items)

        # Stats overlay
        stat_items = []
        for s in stats:
            # Same non-object guard as `build_indicators`.
            if not isinstance(s, dict):
                s = {"value": s}
            style_class = s.get("style", "")
            stat_items.append(f"""
        <div class="mstat">
          <span class="mstat-val {esc(style_class)}">{esc(s.get("value", ""))}</span>
          <span class="mstat-lbl">{esc(s.get("label", ""))}</span>
        </div>""")
        stats_html = "\n".join(stat_items)

        mc_html = ""
        if market_caption_text:
            mc_html = f"""
      <div class="market-caption">
        {esc(market_caption_text).replace(chr(10), "<br/>")}
      </div>
"""

        bar_html = f"""
    <div class="vis-banner vis-markets">
      <div class="chart-wrap">
{bars_joined}
      </div>
      <div class="market-overlay"></div>
      <div class="market-stats">
{stats_html}
      </div>
{mc_html}
    </div>
"""

    caption_html = f'    <div class="img-cap">{esc(caption)}</div>\n' if caption else ""

    body_html = markdown_to_html(body)

    # Pullquote
    pq_html = ""
    if pullquote:
        pq_text = esc(pullquote.get("text", ""))
        pq_attr = esc(pullquote.get("attribution", ""))
        pq_html = f"""
    <div class="pullquote">
      <div class="pq-bg-num">&ldquo;</div>
      <div class="pq-text">&ldquo;{pq_text}&rdquo;</div>
      <div class="pq-attr">{pq_attr}</div>
    </div>
"""

    return f"""
  <div class="section">
{header}
{bar_html}
{caption_html}
    {body_html}
{pq_html}
  </div>
"""


def build_the_heading(data, section_num=5, brand=None):
    """The Heading section - the company perspective."""
    if not data:
        return ""

    # A dict without a string body falls back to "", never to the dict itself.
    body = data if isinstance(data, str) else str(data.get("body", "") or "")
    header = build_section_header(section_num, *section_label(brand or {}, "the_heading", "Our Perspective", "The Heading"))

    body_html = markdown_to_html(body)
    # First paragraph gets .lede class
    body_html = body_html.replace("<p>", '<p class="lede">', 1)

    return f"""
  <div class="section">
{header}
    {body_html}
  </div>
"""


def build_signal_watch(items, section_num=6, brand=None):
    """Signal Watch section with numbered table."""
    if not items:
        return ""

    header = build_section_header(section_num, *section_label(brand or {}, "signal_watch", "Forward Indicators", "Signal Watch"))

    rows = []
    for i, item in enumerate(items, 1):
        # Support markdown bold in signal items; `esc` coerces a bare number.
        item_html = esc(item)
        item_html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", item_html)
        rows.append(f"""
      <div class="signal-row">
        <div class="sig-idx">{i:02d}</div>
        <div class="sig-text">{item_html}</div>
        <div class="sig-dot"><span></span></div>
      </div>
""")

    return f"""
  <div class="section">
{header}
    <div class="signal-table">
{"".join(rows)}
    </div>
  </div>
"""


def build_recommended_reading(items, section_num=7, brand=None):
    """Recommended reading section with numbered list."""
    if not items:
        return ""

    header = build_section_header(section_num, *section_label(brand or {}, "recommended_reading", "Further Reading", "Recommended"))

    read_items = []
    for i, item in enumerate(items, 1):
        # A scalar entry is the title; `safe_url` answers `#` for the absent url.
        if not isinstance(item, dict):
            item = {"title": item}
        title = esc(item.get("title", ""))
        url = safe_url(item.get("url"))
        source = esc(item.get("source", ""))
        desc = esc(item.get("description", ""))
        source_line = source
        if desc:
            source_line += f" &nbsp;&middot;&nbsp; {desc}"
        read_items.append(f"""
      <div class="read-item">
        <span class="read-n">{i:02d}</span>
        <div>
          <a class="read-a" href="{url}" target="_blank">{title}</a>
          <div class="read-src">{source_line}</div>
        </div>
      </div>
""")

    return f"""
  <div class="section">
{header}
    <div class="reading">
{"".join(read_items)}
    </div>
  </div>
"""


def build_footer(logo_uri, issue_num, display_date, brand=None, year=None):
    """Footer with logo, copyright, tagline, and issue number, all from the brand."""
    brand = brand or {}
    logo_html = ""
    if logo_uri:
        logo_html = f'<img src="{logo_uri}" alt="Logo" class="logo-footer"/>'

    issue_str = f"{issue_num:03d}" if isinstance(issue_num, int) else esc(issue_num)

    # The default name is a placeholder, never a copyright holder.
    name = brand_text(brand, "name")
    holder = brand_text(brand, "copyright_holder") or (name if name != DEFAULT_BRAND["name"] else "")
    website = brand_text(brand, "website")
    copy_parts = [f"&copy; {esc(year or datetime.now(get_default_tz()).year)} {esc(holder)}".rstrip()] if holder else []
    if website.lower().startswith("https://"):
        label = website[len("https://"):].rstrip("/")
        copy_parts.append(f'<a href="{safe_url(website)}">{esc(label)}</a>')
    copy_html = (f'<div class="ft-copy">{" &nbsp;&middot;&nbsp; ".join(copy_parts)}</div>'
                 if copy_parts else "")
    tagline = brand_text(brand, "tagline")
    tag_html = f'<div class="ft-tag">{esc(tagline)}</div>' if tagline else ""

    return f"""
<div class="footer">
  <div>
    {logo_html}
    {copy_html}
    {tag_html}
  </div>
  <div class="ft-right">
    <span class="ft-iss">{issue_str}</span>
    <div class="ft-date">{esc(display_date)}</div>
  </div>
</div>
"""


# ============================================================
# CLI / Main
# ============================================================
def generate_newsletter(data, image_paths=None):
    """Generate the complete HTML newsletter from structured JSON data."""
    image_paths = image_paths or {}
    brand = load_brand()
    logo = logo_path(brand)
    # `load_logo_base64(None)` would fall back to its own default logo, so no
    # data repository is answered here, with no logo.
    logo_uri = load_logo_base64(logo) if logo is not None else ""
    date_str = data.get("date", datetime.now(get_default_tz()).strftime("%Y-%m-%d"))
    issue_num = data.get("issue_number", 1)
    threat_level = data.get("threat_level", "ELEVATED")
    regions = data.get("regions") or brand.get("regions") or []
    if not isinstance(regions, list):
        regions = [regions]

    # Format display date
    try:
        # `frontmatter_date` coerces with str() and reads an ISO datetime too.
        dt = frontmatter_date(date_str)
        display_date = dt.strftime("%d %B %Y")
        year = dt.year
    except ValueError:
        display_date = str(date_str)
        year = None

    css = build_css(brand)
    top_bar = build_top_bar(regions, brand)
    masthead = build_masthead(logo_uri, display_date, issue_num, regions, threat_level, brand)
    hero = build_hero(data.get("hero"))
    indicators = build_indicators(data.get("indicators"))

    # Resolve sea state image
    sea_image_uri = None
    if "sea_state" in image_paths:
        sea_image_uri = embed_image(image_paths["sea_state"])

    # Build content sections
    section_num = 1
    sections = []

    if data.get("sea_state"):
        sections.append(build_sea_state(data["sea_state"], section_num, sea_image_uri, brand=brand))
        section_num += 1

    if data.get("cyber_front"):
        sections.append(build_cyber_front(data["cyber_front"], section_num, brand=brand))
        section_num += 1

    if data.get("navigation_chart"):
        sections.append(build_navigation_chart(data["navigation_chart"], section_num, brand=brand))
        section_num += 1

    if data.get("market_depth"):
        sections.append(build_market_depth(data["market_depth"], section_num, brand=brand))
        section_num += 1

    if data.get("the_heading"):
        sections.append(build_the_heading(data["the_heading"], section_num, brand=brand))
        section_num += 1

    if data.get("signal_watch"):
        sections.append(build_signal_watch(data["signal_watch"], section_num, brand=brand))
        section_num += 1

    if data.get("recommended_reading"):
        sections.append(build_recommended_reading(data["recommended_reading"], section_num, brand=brand))
        section_num += 1

    sections_html = "\n".join(sections)

    footer = build_footer(logo_uri, issue_num, display_date, brand, year)

    fonts_url = brand_text(brand, "fonts_css_url")
    fonts_html = ""
    if fonts_url.lower().startswith("https://"):
        if fonts_url.lower().startswith("https://fonts.googleapis.com/"):
            fonts_html = ('<link rel="preconnect" href="https://fonts.googleapis.com"/>\n'
                          '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>\n')
        fonts_html += f'<link href="{safe_url(fonts_url)}" rel="stylesheet"/>'
    elif fonts_url:
        print(f"Warning: fonts_css_url {fonts_url!r} is not https; web fonts skipped",
              file=sys.stderr)

    newsletter_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Intelligence Briefing &mdash; {esc(display_date)}</title>
{fonts_html}
<style>
{css}
</style>
</head>
<body>
<div class="page">

<!-- TOP BAR -->
{top_bar}

<!-- MASTHEAD -->
{masthead}

  <!-- HERO -->
{hero}

  <!-- INDICATORS -->
{indicators}

<!-- CONTENT -->
<div class="content">
{sections_html}
</div><!-- /content -->

<!-- FOOTER -->
{footer}

</div><!-- /page -->
</body>
</html>"""

    return newsletter_html


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def safe_date_segment(raw):
    """An ISO date safe to use as one path segment. Missing -> today.

    `date.fromisoformat` alone is not enough: on 3.11 it also accepts the
    compact "20260824" form, which is a different string and a different
    directory. The shape is checked first, then the calendar.
    """
    if raw is None:
        return datetime.now(get_default_tz()).strftime("%Y-%m-%d")
    text = str(raw).strip()
    if not _ISO_DATE_RE.match(text):
        raise SystemExit(f"date {raw!r} is not YYYY-MM-DD; refusing to use it "
                         f"as an output directory")
    try:
        date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
    except ValueError as exc:
        raise SystemExit(f"date {raw!r} is not a real date: {exc}") from exc
    return text


def count_words(html_doc):
    """Editorial words in a rendered newsletter.

    `strip_html` drops `<style>` and `<script>` bodies (the inlined CSS is not
    prose); `word_count` is the workspace's one definition of a word.
    """
    return word_count(strip_html(html_doc))


def generate_pdf(html_path, pdf_path):
    """Generate a single-page PDF from the HTML newsletter using Playwright."""
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Warning: playwright not installed. Skipping PDF generation.")
        print("  Install with: pip install playwright && python -m playwright install chromium")
        return False

    # Path.as_uri() builds the file URI correctly on both platforms.
    file_url = Path(html_path).resolve().as_uri()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent=user_agent())
            page.goto(file_url)
            # Bounded and non-fatal: the remote Google Fonts link never settles offline,
            # and the local page is fully laid out without it.
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except PlaywrightTimeoutError:
                print("Warning: the page never went network-idle within 5s. On "
                      "an offline or firewalled host the remote Google Fonts "
                      "stylesheet is the cause. Rendering the PDF with the "
                      "fonts that did load.", file=sys.stderr)

            # Measure full page height for single-page output
            height = page.evaluate("document.documentElement.scrollHeight")

            page.pdf(
                path=str(pdf_path),
                width="750px",
                height=f"{height + 40}px",
                print_background=True,
                margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
            )
            browser.close()
        print(f"PDF generated:  {pdf_path}")
        print(f"PDF size:       {Path(pdf_path).stat().st_size:,} bytes")
        return True
    except Exception as e:
        print(f"Warning: PDF generation failed: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Intelligence Briefing HTML Generator (V3)")
    parser.add_argument("input_json", help="Path to input JSON file")
    parser.add_argument("--output-dir", help="Output directory (default: YYYY-MM-DD/ under data_path('newsletters'))")
    parser.add_argument("--images", nargs="*", help="Image mappings: section=path (e.g. sea_state=/path/to/img.png)")
    parser.add_argument("--no-pdf", action="store_true", help="Skip PDF generation")
    args = parser.parse_args()

    input_path = Path(args.input_json)
    if not input_path.exists():
        print(f"Error: Input file not found: {input_path}")
        sys.exit(1)

    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # `json.load` accepts any JSON value; only an object is a newsletter.
    if not isinstance(data, dict):
        print(f"Error: {input_path} must hold a JSON object, not a "
              f"{type(data).__name__}", file=sys.stderr)
        sys.exit(1)

    # Parse image paths
    image_paths = {}
    if args.images:
        # `--images` takes `section=path`; a mapping without `=` is refused, not dropped.
        malformed = [m for m in args.images if "=" not in m]
        if malformed:
            print(f"Error: --images takes section=path. Not understood: "
                  f"{', '.join(repr(m) for m in malformed)}", file=sys.stderr)
            sys.exit(2)
        for mapping in args.images:
            section, path = mapping.split("=", 1)
            image_paths[section] = path
        # A section nothing renders is refused too, never silently ignored.
        unknown = sorted(set(image_paths) - IMAGE_SECTIONS)
        if unknown:
            print(f"Error: --images knows only "
                  f"{', '.join(sorted(IMAGE_SECTIONS))}. Not rendered: "
                  f"{', '.join(repr(s) for s in unknown)}", file=sys.stderr)
            sys.exit(2)

    # Determine output directory. `date` becomes a path segment, so it must be a real date.
    date_str = safe_date_segment(data.get("date"))
    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        output_dir = newsletters_dir() / date_str
    output_dir.mkdir(parents=True, exist_ok=True)

    output_path = output_dir / "intelligence-briefing.html"

    newsletter_html = generate_newsletter(data, image_paths)
    output_path.write_text(newsletter_html, encoding="utf-8")

    # Not named `word_count`: that is the imported function, and a local of the
    # same name in this scope is a trap for the next edit here.
    words = count_words(newsletter_html)
    print(f"Newsletter generated: {output_path}")
    print(f"Word count: {words}")
    print(f"File size: {output_path.stat().st_size:,} bytes")

    # Generate PDF (single continuous page)
    if not args.no_pdf:
        pdf_path = output_dir / "intelligence-briefing.pdf"
        generate_pdf(output_path, pdf_path)


if __name__ == "__main__":
    main()
