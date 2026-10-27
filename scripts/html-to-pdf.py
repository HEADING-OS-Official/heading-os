#!/usr/bin/env python3
"""Convert an HTML file to PDF using Playwright.

Every failure goes to STDERR; stdout carries the result and nothing else, so a
caller can tell a diagnostic from a result by channel alone. Callers run it as a
subprocess and print stderr on a non-zero exit (render-doctype, docparse, both
dashboard generators and several skills).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.utils.user_agent import user_agent  # noqa: E402

def main():
    if len(sys.argv) < 2:
        print("Usage: python html-to-pdf.py <input.html> [output.pdf]",
              file=sys.stderr)
        sys.exit(1)

    html_path = Path(sys.argv[1]).resolve()
    if not html_path.exists():
        print(f"[ERROR] Input file not found: {html_path}", file=sys.stderr)
        sys.exit(1)

    if len(sys.argv) >= 3:
        pdf_path = sys.argv[2]
    else:
        pdf_path = str(html_path.with_suffix(".pdf"))

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        print(f"[ERROR] Cannot import playwright.sync_api: {e}", file=sys.stderr)
        print("[HINT] If 'playwright' itself is missing: pip install playwright && playwright install chromium",
              file=sys.stderr)
        print("[HINT] If a transitive dep (e.g. 'greenlet') is missing: pip install <name> -- check requirements.txt",
              file=sys.stderr)
        sys.exit(1)

    # `Path.as_uri()`, not a hand-built string, so a UNC input stays loadable.
    file_url = html_path.resolve().as_uri()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent=user_agent())
            page.goto(file_url, timeout=30000)
            page.wait_for_load_state("networkidle", timeout=30000)

            page.pdf(
                path=pdf_path,
                format="A4",
                print_background=True,
                margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
            )
            browser.close()
    except Exception as e:
        print(f"[ERROR] PDF generation failed: {e}", file=sys.stderr)
        sys.exit(1)

    size = Path(pdf_path).stat().st_size
    print(f"PDF generated: {pdf_path}")
    print(f"Size: {size:,} bytes")

if __name__ == "__main__":
    main()
