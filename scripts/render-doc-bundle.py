#!/usr/bin/env python3
"""Render a directory of markdown files as one standalone HTML document.

A task that produces several markdown files in one timestamped directory has a
document already; what it lacks is a way to read it as one. This makes that:
a cover, a contents list, and one part per file, in one file that opens from
disk with the network off and prints from the browser with page breaks between
parts.

Usage:

    python scripts/render-doc-bundle.py <directory>
    python scripts/render-doc-bundle.py <directory> --title "..." --out doc.html
    python scripts/render-doc-bundle.py <directory> --stdout > elsewhere.html

READING ORDER. Alphabetical is almost never the order a document should be read
in, so the directory may carry an `ORDER.txt`: one markdown filename per line,
blank lines and `#` comments ignored, and an optional gloss after a tab or two
spaces. Files named there come first in that order; anything else follows
alphabetically rather than being dropped, because a file silently left out of a
document is the failure this would otherwise introduce. The run says on stderr
how many files were ordered and how many were appended.

PROVENANCE MARKERS. A paragraph beginning in bold with `RE-DERIVED`, `CARRIED`
or `DATED HISTORY` is given a coloured rule. Those three labels are this
workspace's convention for saying whether a figure was measured now, carried in
from somewhere else, or kept only as history, and seeing which is which at a
glance is most of why a long evidence document is worth reading. Nothing is
inferred: a document that does not use the labels gets no colour, and `--strict`
turns a document with no markers at all into a non-zero exit rather than a page
that quietly lost its highlighting.

SELF-CONTAINED, ON PURPOSE. No webfont, no stylesheet link, no script tag. A
document that needs the network to render is a document that stops rendering,
and this one is written into the private overlay where it may outlive any host
it could have fetched from. Every run checks it and exits 3 on any external
reference.

WHERE IT WRITES. Wherever it is pointed, which in normal use is the same
directory it read. It never joins a path against the engine checkout; the
caller supplies the directory, and a relative one resolves under the data root
when it names a directory inside it, else against the current directory.
"""
from __future__ import annotations

import argparse
import html
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.atomic import atomic_write_text  # noqa: E402
from scripts.utils.colors import GRAY, GREEN, RED, RESET, YELLOW  # noqa: E402
from scripts.utils.workspace import get_data_root, get_default_tz  # noqa: E402

ORDER_FILE = "ORDER.txt"

#: The three provenance labels and the class each gets. Data, not code, so a
#: fourth label is one row. Spelled in the order of how much weight a reader
#: should give the figure.
PROVENANCE = (
    ("RE-DERIVED", "prov-derived"),
    ("CARRIED", "prov-carried"),
    ("DATED HISTORY", "prov-history"),
)

CSS = """
:root {
  --ink: #16191d;
  --ink-soft: #4a525c;
  --ink-faint: #7a828c;
  --rule: #d9dde2;
  --rule-soft: #eceff2;
  --paper: #ffffff;
  --wash: #f6f7f9;
  --accent: #1f4e79;
  --accent-soft: #eaf0f6;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--wash); color: var(--ink);
  font: 16px/1.62 "Charter", "Bitstream Charter", "Iowan Old Style", Georgia,
        "Times New Roman", serif;
  font-feature-settings: "kern" 1, "liga" 1, "onum" 1;
}
.sheet {
  max-width: 47rem; margin: 0 auto; background: var(--paper);
  padding: 4.5rem 4rem 6rem;
  box-shadow: 0 1px 3px rgba(0,0,0,.07), 0 12px 32px rgba(0,0,0,.05);
}
.cover { border-bottom: 2px solid var(--ink); padding-bottom: 2.5rem; margin-bottom: 2.5rem; }
.eyebrow {
  font: 600 11px/1 ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif;
  letter-spacing: .16em; text-transform: uppercase; color: var(--accent);
  margin-bottom: 1.4rem;
}
h1.doc { font-size: 2.45rem; line-height: 1.15; margin: 0 0 .7rem; font-weight: 600; letter-spacing: -.012em; }
.subtitle { font-size: 1.12rem; color: var(--ink-soft); margin: 0 0 2rem; }
.meta {
  display: grid; grid-template-columns: max-content 1fr; gap: .38rem 1.6rem;
  font: 13px/1.5 ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif;
}
.meta dt { color: var(--ink-faint); text-transform: uppercase; letter-spacing: .07em; font-size: 11px; font-weight: 600; }
.meta dd { margin: 0; color: var(--ink-soft); }
.toc { margin: 0 0 3.5rem; padding: 1.6rem 1.8rem; background: var(--wash); border: 1px solid var(--rule-soft); }
.toc h2 {
  font: 600 11px/1 ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif;
  letter-spacing: .16em; text-transform: uppercase; color: var(--ink-faint); margin: 0 0 1rem;
}
.toc ol { margin: 0; padding: 0; list-style: none; counter-reset: toc; }
.toc li { counter-increment: toc; margin: .5rem 0; display: flex; gap: .8rem; align-items: baseline; }
.toc li::before {
  content: counter(toc, decimal-leading-zero);
  font: 600 11px/1 ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  color: var(--accent); flex: 0 0 auto; padding-top: .15rem;
}
.toc a { color: var(--ink); text-decoration: none; font-weight: 600; }
.toc a:hover { color: var(--accent); }
.toc .gloss { color: var(--ink-faint); font-size: .9rem; display: block; font-weight: 400; }
section.part { margin-top: 4rem; padding-top: 2.2rem; border-top: 1px solid var(--rule); }
section.part:first-of-type { border-top: 0; }
.part-no {
  font: 600 11px/1 ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  letter-spacing: .1em; color: var(--accent); display: block; margin-bottom: .55rem;
}
h2.part-title { font-size: 1.85rem; line-height: 1.2; margin: 0 0 .35rem; font-weight: 600; letter-spacing: -.01em; }
.part-gloss { color: var(--ink-faint); margin: 0 0 2.2rem; font-size: .97rem; }
.body h1 { font-size: 1.5rem; margin: 2.6rem 0 .9rem; font-weight: 600; line-height: 1.25; }
.body h2 { font-size: 1.25rem; margin: 2.3rem 0 .8rem; font-weight: 600; line-height: 1.3; }
.body h3 { font-size: 1.06rem; margin: 1.9rem 0 .6rem; font-weight: 600; }
.body h4 { font-size: .95rem; margin: 1.5rem 0 .5rem; font-weight: 600; color: var(--ink-soft); }
.body h1:first-child, .body h2:first-child { margin-top: 0; }
.body p { margin: 0 0 1.05rem; }
.body ul, .body ol { margin: 0 0 1.15rem; padding-left: 1.45rem; }
.body li { margin: .32rem 0; }
.body li > ul, .body li > ol { margin: .3rem 0; }
.body strong { font-weight: 600; }
.body a { color: var(--accent); }
.body hr { border: 0; border-top: 1px solid var(--rule-soft); margin: 2.2rem 0; }
.body blockquote {
  margin: 1.4rem 0; padding: .9rem 1.3rem; background: var(--accent-soft);
  border-left: 3px solid var(--accent); color: var(--ink-soft);
}
.body blockquote p:last-child { margin-bottom: 0; }
.body code {
  font: 13.5px/1.5 ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  background: var(--wash); border: 1px solid var(--rule-soft);
  padding: .08em .34em; border-radius: 3px; word-break: break-word;
}
.body pre {
  background: var(--wash); border: 1px solid var(--rule-soft);
  padding: 1rem 1.15rem; overflow-x: auto; margin: 0 0 1.3rem;
  font-size: 13px; line-height: 1.55;
}
.body pre code { background: none; border: 0; padding: 0; font-size: inherit; }
.body table {
  border-collapse: collapse; width: 100%; margin: 0 0 1.5rem;
  font: 14px/1.5 ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif;
}
.body th, .body td { border: 1px solid var(--rule); padding: .5rem .7rem; text-align: left; vertical-align: top; }
.body th {
  background: var(--wash); font-weight: 600; font-size: 12.5px;
  text-transform: uppercase; letter-spacing: .04em; color: var(--ink-soft);
}
.body tbody tr:nth-child(even) td { background: #fbfcfd; }
.body p.prov-derived { background: #eef6ef; border-left: 3px solid #5a8f63; padding: .75rem 1.2rem; margin: 1.3rem 0; }
.body p.prov-carried { background: #fdf6e8; border-left: 3px solid #e0c684; padding: .75rem 1.2rem; margin: 1.3rem 0; }
.body p.prov-history { background: var(--wash); border-left: 3px solid var(--rule); padding: .75rem 1.2rem; margin: 1.3rem 0; color: var(--ink-soft); }
footer.doc {
  margin-top: 5rem; padding-top: 1.6rem; border-top: 1px solid var(--rule);
  font: 12px/1.6 ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif;
  color: var(--ink-faint);
}
@media (max-width: 52rem) {
  .sheet { padding: 2.5rem 1.5rem 4rem; }
  h1.doc { font-size: 1.9rem; }
}
@media print {
  body { background: #fff; font-size: 10.5pt; }
  .sheet { box-shadow: none; max-width: none; padding: 0; }
  section.part { break-before: page; }
  section.part:first-of-type { break-before: avoid; }
  h1, h2, h3, h4 { break-after: avoid; }
  pre, table, blockquote { break-inside: avoid; }
  .toc { break-after: page; }
  a { color: inherit; text-decoration: none; }
}
"""


class BundleError(RuntimeError):
    """The directory cannot be rendered, with the reason in the message."""


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "part"


def read_order(directory: Path) -> list[tuple[str, str]]:
    """`(filename, gloss)` from `ORDER.txt`, or an empty list if there is none.

    A gloss is whatever follows a tab or two spaces on the line. It is the one
    piece of information a directory listing cannot carry: what the file is FOR,
    in the reader's terms rather than the author's filename.
    """
    path = directory / ORDER_FILE
    if not path.is_file():
        return []
    rows: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"\t|\s{2,}", line, maxsplit=1)
        rows.append((parts[0].strip(), parts[1].strip() if len(parts) > 1 else ""))
    return rows


def plan(directory: Path) -> tuple[list[tuple[Path, str]], int, int]:
    """Which files, in which order, with their glosses.

    Returns `(rows, ordered, appended)`. Anything not named in `ORDER.txt` is
    APPENDED alphabetically rather than dropped: a document that silently omits
    a file its author wrote is worse than one whose order is imperfect.
    """
    present = sorted(p for p in directory.glob("*.md") if p.is_file())
    if not present:
        raise BundleError(f"no markdown files in {directory}")

    by_name = {p.name: p for p in present}
    rows: list[tuple[Path, str]] = []
    named: set[str] = set()
    missing: list[str] = []
    for name, gloss in read_order(directory):
        if name not in by_name:
            missing.append(name)
            continue
        rows.append((by_name[name], gloss))
        named.add(name)
    if missing:
        raise BundleError(
            f"{ORDER_FILE} names {missing}, which {directory} does not hold. "
            f"A reading order that points at a file nobody wrote is a document "
            f"missing a part, so this refuses rather than rendering the rest.")

    appended = [p for p in present if p.name not in named]
    rows.extend((p, "") for p in appended)
    return rows, len(named), len(appended)


def title_of(path: Path, text: str) -> str:
    """The file's own leading H1, else its filename made readable."""
    match = re.match(r"\A\s*#\s+(.+?)\s*$", text, flags=re.MULTILINE)
    if match:
        return match.group(1).strip()
    return path.stem.replace("-", " ").replace("_", " ").title()


def render_body(text: str) -> tuple[str, int]:
    """One file's markdown to HTML, with its leading H1 dropped.

    The H1 goes because the section heading already carries it; printing the
    same sentence twice reads as a mistake rather than as structure. Returns the
    HTML and how many provenance paragraphs were marked, so the caller can
    assert on a number instead of trusting that a regex fired.
    """
    import markdown

    stripped = re.sub(r"\A\s*#\s+.*?\n", "", text, count=1)
    body = markdown.markdown(
        stripped,
        extensions=["tables", "fenced_code", "sane_lists", "attr_list"],
        output_format="html5",
    )
    marked = 0
    for label, css_class in PROVENANCE:
        body, count = re.subn(rf"<p>(?=<strong>{label}\b)",
                              f'<p class="{css_class}">', body)
        marked += count
    return body, marked


def build(directory: Path, *, title: str, subtitle: str, eyebrow: str) -> tuple[str, dict]:
    """The whole document, and what was counted while building it."""
    rows, ordered, appended = plan(directory)
    tz = get_default_tz()
    now = datetime.now(tz)
    newest = datetime.fromtimestamp(
        max(p.stat().st_mtime for p, _ in rows), tz)

    words = 0
    marked = 0
    toc: list[str] = []
    parts: list[str] = []
    seen: set[str] = set()
    for index, (path, gloss) in enumerate(rows, start=1):
        text = path.read_text(encoding="utf-8")
        words += len(text.split())
        part_title = title_of(path, text)
        anchor = slug(part_title)
        # A duplicate anchor silently sends two contents entries to one place.
        while anchor in seen:
            anchor = f"{anchor}-{index}"
        seen.add(anchor)
        body, part_marked = render_body(text)
        marked += part_marked
        toc.append(
            f'      <li><a href="#{anchor}">{html.escape(part_title)}'
            + (f'<span class="gloss">{html.escape(gloss)}</span>' if gloss else "")
            + "</a></li>")
        parts.append(
            f'    <section class="part" id="{anchor}">\n'
            f'      <span class="part-no">Part {index:02d}</span>\n'
            f'      <h2 class="part-title">{html.escape(part_title)}</h2>\n'
            + (f'      <p class="part-gloss">{html.escape(gloss)}</p>\n' if gloss else "")
            + f'      <div class="body">\n{body}\n      </div>\n'
            f'    </section>')

    newline = "\n"
    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{CSS}</style>
</head>
<body>
<article class="sheet">
  <header class="cover">
    <div class="eyebrow">{html.escape(eyebrow)}</div>
    <h1 class="doc">{html.escape(title)}</h1>
{f'    <p class="subtitle">{html.escape(subtitle)}</p>{newline}' if subtitle else ''}\
    <dl class="meta">
      <dt>Source</dt><dd>{html.escape(directory.name)}</dd>
      <dt>Written</dt><dd>{newest:%Y-%m-%d %H:%M}</dd>
      <dt>Rendered</dt><dd>{now:%Y-%m-%d %H:%M}</dd>
      <dt>Length</dt><dd>{words:,} words across {len(rows)} parts</dd>
    </dl>
  </header>

  <nav class="toc">
    <h2>Contents</h2>
    <ol>
{newline.join(toc)}
    </ol>
  </nav>

{newline.join(parts)}

  <footer class="doc">
    Generated from the markdown files in {html.escape(directory.name)}.
    Regenerate rather than edit: an edit here is lost on the next render.
  </footer>
</article>
</body>
</html>
"""
    return doc, {"parts": len(rows), "ordered": ordered, "appended": appended,
                 "words": words, "marked": marked}


EXTERNAL_REF = re.compile(r'(?:src|href)\s*=\s*"(?!#)')


def external_refs(doc: str) -> int:
    """How many references would need something other than this file."""
    return len(EXTERNAL_REF.findall(doc))


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="render-doc-bundle.py",
        description="Render a directory of markdown files as one HTML document.")
    parser.add_argument("directory", type=Path,
                        help=("the directory of markdown files. A relative path "
                              "resolves under the data root when it names a "
                              "directory inside it, else against the current "
                              "directory."))
    parser.add_argument("--title", default=None,
                        help="document title (default: the directory name)")
    parser.add_argument("--subtitle", default="")
    parser.add_argument("--eyebrow", default="HEADING OS",
                        help="the small line above the title")
    parser.add_argument("--out", type=Path, default=None,
                        help=("output file (default: <directory>/<name>.html). "
                              "Ignored with --stdout."))
    parser.add_argument("--stdout", action="store_true",
                        help="write the document to stdout instead of a file")
    parser.add_argument("--strict", action="store_true",
                        help=("fail when no provenance paragraph was marked. Use "
                              "it on a document that is supposed to carry them, "
                              "so a silent styling regression is a red run."))
    args = parser.parse_args()

    directory = args.directory
    if not directory.is_absolute():
        # Contained: a `../` that climbs out of the data root is not taken
        # from here, and falls through to the current directory like any other
        # relative path that names nothing under the data root.
        data_root = get_data_root().resolve()
        candidate = (data_root / directory).resolve()
        inside = candidate.is_relative_to(data_root)
        directory = candidate if inside and candidate.is_dir() else directory.resolve()

    if not directory.is_dir():
        print(f"{RED}render-doc-bundle: not a directory: {directory}{RESET}",
              file=sys.stderr)
        return 2

    title = args.title or directory.name.replace("_", " ").replace("-", " ")
    try:
        doc, counts = build(directory, title=title, subtitle=args.subtitle,
                            eyebrow=args.eyebrow)
    except BundleError as exc:
        print(f"{RED}render-doc-bundle: {exc}{RESET}", file=sys.stderr)
        return 2

    refs = external_refs(doc)
    if refs:
        print(f"{RED}render-doc-bundle: {refs} external reference(s) in the "
              f"document. It must open with the network off.{RESET}",
              file=sys.stderr)
        return 3
    if args.strict and not counts["marked"]:
        print(f"{RED}render-doc-bundle: --strict, and no provenance paragraph "
              f"was marked. The labels are "
              f"{', '.join(label for label, _ in PROVENANCE)}, in bold at the "
              f"start of a paragraph.{RESET}", file=sys.stderr)
        return 4

    if args.stdout:
        sys.stdout.write(doc)
        destination = "stdout"
    else:
        out = args.out or (directory / f"{directory.name}.html")
        atomic_write_text(out, doc)
        destination = str(out)

    print(f"{GREEN}{destination}{RESET}", file=sys.stderr)
    print(f"{GRAY}{len(doc):,} bytes, {counts['words']:,} words, "
          f"{counts['parts']} part(s), {counts['marked']} provenance "
          f"paragraph(s){RESET}", file=sys.stderr)
    if counts["appended"]:
        print(f"{YELLOW}{counts['appended']} file(s) were not named in "
              f"{ORDER_FILE} and were appended alphabetically.{RESET}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
