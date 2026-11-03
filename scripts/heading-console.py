#!/usr/bin/env python3
"""heading-console.py: the HEADING OS Admin Console, one static page on this computer (HOS-333).

The page lists every web interface of the install, from the one list in
`scripts/utils/web_interfaces.py`, with the user guides of the engine's
`docs/` rendered beside it. It is built here and opened from disk: no server,
no port, no daemon, and no request to anything outside this computer. The
terminal twin of the page is `list`.

Where it goes: `runtime:admin-console/cache` of the data repository,
`index.html` and `guides/<name>.html`, each written through a `.tmp` file and
`os.replace`. A data repository whose registry has no admin-console row is
refused with the registry's own message.

The page's one script asks each address again when the page opens, with a
no-cors fetch: a resolved request reads as answering, a rejected one as not.
A browser that refuses a file page's request to 127.0.0.1 makes every check
reject; the page says so in one line under the cards.

No secret reaches the page: a sign-in line names the setting and the file
that holds it, never the value.

Usage:
    .venv/bin/python scripts/heading-console.py build [--engine PATH]
    .venv/bin/python scripts/heading-console.py open  [--engine PATH]
    .venv/bin/python scripts/heading-console.py list  [--engine PATH]

`--engine` names the engine whose docs/, .env path (for the CLIProxyAPI
sign-in line) and time zone are used; the modules imported are always this
file's own engine.

Exit codes: 0 done, 1 refused or failed (one line on stderr, no traceback).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import os
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))

from scripts.utils import install_answers  # noqa: E402 - the one reader of the install's answers
from scripts.utils.console_logo import LOGO_PNG_BASE64, LOGO_SHA256  # noqa: E402
from scripts.utils.data_path import DataPathError, data_path, data_write_dir  # noqa: E402
from scripts.utils.paths import DataRootError  # noqa: E402
from scripts.utils.web_interfaces import WebInterface, answers, web_interfaces  # noqa: E402

TITLE = "HEADING OS Admin Console"
BROWSER_NOTE = ("Live status comes from your browser; if every system shows not answering now while the "
                "build time says answering, your browser blocks the check, and the build-time status is "
                "the one to trust.")

STYLE = """<style>
body { font-family: system-ui, sans-serif; margin: 0; background: #f4f5f7; color: #1d2330; }
header { display: flex; align-items: center; gap: 16px; padding: 20px 32px; background: #0f1a2b; color: #fff; }
header img { height: 56px; }
header h1 { font-size: 1.4em; margin: 0; font-weight: 600; }
main { max-width: 960px; margin: 0 auto; padding: 24px 32px 48px; }
h2 { font-size: 1.1em; margin: 28px 0 12px; }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; }
.card { background: #fff; border: 1px solid #d9dde4; border-radius: 8px; padding: 16px; }
.card h3 { margin: 0 0 6px; font-size: 1.05em; }
.card p { margin: 6px 0; }
.purpose { color: #4a5262; }
.signin { font-size: 0.9em; color: #4a5262; }
.dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; }
.dot.up { background: #2e9d57; }
.dot.down { background: #c94a3b; }
.note { font-size: 0.9em; color: #4a5262; margin-top: 16px; }
footer { font-size: 0.85em; color: #6b7280; margin-top: 32px; }
a { color: #1c5fb8; }
pre, code { background: #eef0f3; border-radius: 4px; }
pre { padding: 10px; overflow-x: auto; }
table { border-collapse: collapse; }
td, th { border: 1px solid #d9dde4; padding: 4px 8px; }
</style>"""

#: The page's only script. A constant: no value is interpolated into it; each
#: address reaches it through the dot's data-url attribute.
LIVE_CHECK = """<script>
(function () {
  var dots = document.querySelectorAll(".dot[data-url]");
  for (var i = 0; i < dots.length; i++) {
    (function (dot) {
      var label = dot.parentNode.querySelector(".state");
      try {
        fetch(dot.dataset.url, {mode: "no-cors", cache: "no-store"}).then(function () {
          dot.className = "dot up";
          label.textContent = "answering now";
        }, function () {
          dot.className = "dot down";
          label.textContent = "not answering now";
        });
      } catch (e) {
        label.textContent += " (live check blocked by the browser)";
      }
    })(dots[i]);
  }
})();
</script>"""


class ConsoleError(RuntimeError):
    """An expected failure: one line on stderr, exit 1."""


def esc(text: str) -> str:
    return html.escape(text, quote=True)


def is_wsl() -> bool:
    # The is_wsl test of scripts/heading-install.py, restated: this file does not import the installer.
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except OSError:
        return False


def windows_path(path: Path) -> tuple[str | None, str]:
    """(the Windows form of `path`, "") or (None, why not)."""
    try:
        proc = subprocess.run(["wslpath", "-w", str(path)], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    win = proc.stdout.strip()
    if proc.returncode != 0 or not win:
        return None, (proc.stderr.strip() or f"wslpath exited {proc.returncode}")[:200]
    return win, ""


def zone_of(engine: Path):
    """The engine's zone (HEADING_OS_TZ), else UTC."""
    name = install_answers.timezone(engine)
    return ZoneInfo(name) if name else timezone.utc


def logo_data_uri() -> str:
    data = base64.b64decode(LOGO_PNG_BASE64)
    if hashlib.sha256(data).hexdigest() != LOGO_SHA256:
        raise ConsoleError("the logo in scripts/utils/console_logo.py does not match its LOGO_SHA256")
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def page(title: str, body: str, *, script: str = "") -> str:
    return (f'<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f"<title>{esc(title)}</title>\n{STYLE}\n</head>\n<body>\n{body}\n{script}</body>\n</html>\n")


# ---- the guides -------------------------------------------------------------

def guide_title(text: str, stem: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or stem
    return stem


def render_markdown(text: str) -> str:
    """The guide as HTML. Raw HTML in the Markdown is shown as text, never passed through.
    A link whose scheme is not http, https or mailto, or relative, loses its address; an image
    whose address is not a data: URI is replaced by its alt text, so the page requests nothing."""
    import markdown
    from markdown.treeprocessors import Treeprocessor

    def scheme_of(value: str) -> str:
        # The scheme the browser will read: Markdown keeps character references such
        # as &#58; in an attribute, and a browser decodes them and skips tabs and
        # newlines inside a scheme.
        decoded = "".join(ch for ch in html.unescape(value) if ord(ch) > 0x20)
        try:
            return urllib.parse.urlsplit(decoded).scheme.lower()
        except ValueError:
            return "invalid"  # an address urlsplit refuses is unsafe: no link scheme and not data


    class _SafeLinks(Treeprocessor):
        def run(self, root):
            for parent in list(root.iter()):
                for i, el in reversed(list(enumerate(parent))):
                    if el.tag == "img":
                        if scheme_of(el.get("src") or "") != "data":
                            text = (el.get("alt") or "") + (el.tail or "")
                            if i:
                                parent[i - 1].tail = (parent[i - 1].tail or "") + text
                            else:
                                parent.text = (parent.text or "") + text
                            parent.remove(el)
                    elif scheme_of(el.get("href") or "") not in ("", "http", "https", "mailto"):
                        el.set("href", "#")

    md = markdown.Markdown(extensions=["tables", "fenced_code"])
    md.preprocessors.deregister("html_block")
    md.inlinePatterns.deregister("html")
    md.treeprocessors.register(_SafeLinks(md), "heading_safe_links", 0)
    return md.convert(text)


def build_guides(engine: Path, out: Path) -> list[tuple[str, str]]:
    """Render every <engine>/docs/*.md into out/guides/; (link, title) per guide, in name order."""
    docs = sorted((engine / "docs").glob("*.md"))
    guides = out / "guides"
    try:
        guides.mkdir(exist_ok=True)
    except OSError as exc:
        raise ConsoleError(f"could not create {guides}: {exc.strerror or exc}") from exc
    found = []
    for doc in docs:
        try:
            text = doc.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ConsoleError(f"could not read {doc}: {getattr(exc, 'strerror', None) or exc}") from exc
        title = guide_title(text, doc.stem)
        body = (f'<main>\n<p><a href="../index.html">Back to the {esc(TITLE)}</a></p>\n'
                f"{render_markdown(text)}\n</main>")
        target = guides / f"{doc.stem}.html"
        try:
            write_atomic(target, page(title, body))
        except OSError as exc:
            raise ConsoleError(f"could not write {target}: {exc.strerror or exc}") from exc
        found.append(("guides/" + urllib.parse.quote(doc.stem) + ".html", title))
    return found


# ---- the page ---------------------------------------------------------------

def card(wi: WebInterface, up: bool | None, at: str) -> str:
    parts = [f'<section class="card">\n<h3>{esc(wi.name)}</h3>', f'<p class="purpose">{esc(wi.purpose)}</p>']
    if wi.url:
        parts.append(f'<p><a href="{esc(wi.url)}">{esc(wi.url)}</a></p>')
    if wi.sign_in:
        parts.append(f'<p class="signin">{esc(wi.sign_in)}</p>')
    if wi.url:
        state = f"answering at {at}" if up else f"not answering at {at}"
        parts.append(f'<p class="status"><span class="dot {"up" if up else "down"}" data-url="{esc(wi.url)}">'
                     f'</span><span class="state">{state}</span></p>')
    else:
        parts.append('<p class="status">not set up here</p>')
    return "\n".join(parts) + "\n</section>"


def cmd_build(engine: Path) -> int:
    try:
        out = data_write_dir("runtime:admin-console/cache")
    except OSError as exc:
        raise ConsoleError(f"could not create the console folder: {exc.strerror or exc}") from exc
    logo = logo_data_uri()
    now = datetime.now(zone_of(engine))
    at = f"{now:%H:%M}"
    cards = [card(wi, answers(wi.url) if wi.url else None, at) for wi in web_interfaces(engine)]
    guides = build_guides(engine, out)
    guide_list = ("\n".join(f'<li><a href="{esc(link)}">{esc(title)}</a></li>' for link, title in guides)
                  or "<li>No guide is installed with this engine.</li>")
    body = (f'<header>\n<img src="{logo}" alt="HEADING OS">\n<h1>{esc(TITLE)}</h1>\n</header>\n<main>\n'
            f'<h2>Systems</h2>\n<div class="cards">\n' + "\n".join(cards) + "\n</div>\n"
            f'<p class="note">{esc(BROWSER_NOTE)}</p>\n'
            f"<h2>Guides</h2>\n<ul>\n{guide_list}\n</ul>\n"
            f"<footer>Built {now:%Y-%m-%d %H:%M} ({esc(str(now.tzinfo))}) by scripts/heading-console.py. "
            f"The same list in a terminal: .venv/bin/python scripts/heading-console.py list</footer>\n</main>")
    index = out / "index.html"
    try:
        write_atomic(index, page(TITLE, body, script=LIVE_CHECK + "\n"))
    except OSError as exc:
        raise ConsoleError(f"could not write {index}: {exc.strerror or exc}") from exc
    print(f"Admin console: {index}")
    if is_wsl():
        win, why = windows_path(index)
        print(f"  In Windows: {win}" if win else f"  (Windows path not available: {why})")
    return 0


def cmd_open(engine: Path) -> int:
    index = data_path("runtime:admin-console/cache") / "index.html"
    if not index.is_file():
        print("The console is not built yet; run: heading-console.py build")
        return 1
    if is_wsl():
        win, why = windows_path(index)
        print(win if win else f"{index}\n(Windows path not available: {why})")
        print("Open this address in your Windows browser.")
        return 0
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    try:
        proc = subprocess.run([opener, str(index)], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConsoleError(f"could not open the page ({exc}); open {index} yourself") from exc
    if proc.returncode != 0:
        why = (proc.stderr.strip() or f"{opener} exited {proc.returncode}")[:200]
        raise ConsoleError(f"could not open the page ({why}); open {index} yourself")
    print(f"Opened {index}")
    return 0


def cmd_list(engine: Path) -> int:
    for wi in web_interfaces(engine):
        if not wi.url:
            print(f"{wi.name}: not set up")
            continue
        state = "answering" if answers(wi.url) else "not answering"
        print(f"{wi.name}: {wi.url} ({state})" + (f"; {wi.sign_in}" if wi.sign_in else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--engine", type=Path, default=ENGINE,
                        help="the engine whose docs, .env and time zone are used (default: this one)")
    ap = argparse.ArgumentParser(description="The HEADING OS Admin Console: one static page of every web "
                                             "interface and the guides.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build", parents=[common], help="build the page into the data repository")
    sub.add_parser("open", parents=[common], help="open the page (on WSL: print its Windows address)")
    sub.add_parser("list", parents=[common], help="the same list in the terminal")
    args = ap.parse_args(argv)
    engine = args.engine.expanduser().resolve()
    try:
        return {"build": cmd_build, "open": cmd_open, "list": cmd_list}[args.cmd](engine)
    except (ConsoleError, DataPathError, DataRootError) as exc:
        print(f"heading-console: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
