#!/usr/bin/env python3
"""Read and render a Figma .fig design-system file.

NAME is a data registry name for a design-system directory. The file read is
the `*.fig` there whose name ends with the latest `YYYY-MM-DD` date.

Usage:
    fig-design.py NAME info
    fig-design.py NAME render --frame "FRAME" [--frame ...] --out DIR
    fig-design.py NAME shells --spec OUT=FRAME[:CHILD-PREFIX] [--spec ...]
                              [--skip-prefix P ...] [--blank-slot S ...] --out DIR
    fig-design.py NAME kit --pages PAGE [PAGE ...] --header TEXT --var-prefix PREFIX --out DIR
    fig-design.py NAME shot --out DIR FILE [FILE ...]

Exit codes: 0 done; 1 something named was not found (the rest was done);
2 a bad, over-limit or missing file, or a refused argument.
"""
from __future__ import annotations

import argparse
import collections
import re
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.data_path import DataPathError, data_path  # noqa: E402
from scripts.utils.paths import DataRootError  # noqa: E402
from scripts.utils.fig_file import FigFileError, K, digest, load  # noqa: E402
from scripts.utils import fig_render  # noqa: E402
from playwright.sync_api import Error as PlaywrightError  # noqa: E402

_DATED_FIG = re.compile(r"(\d{4}-\d{2}-\d{2})\.fig$")
_OUTNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_VAR_PREFIX = re.compile(r"[A-Za-z0-9_-]+")
# Characters that would let a token name or value leave its declaration. A web
# name may legitimately carry a fallback, "--a, rgb(0 0 0 / 50%", so commas,
# spaces, parentheses, "%" and "/" are kept as the file has them.
_CSS_UNSAFE = re.compile(r"[;{}<>\\\n\r]|/\*|\*/")
SPLIT_WIDTH = 4000
SPLIT_MIN_CHILD = 400
MAX_SPLIT_ROUNDS = 64

# Errors a malformed tree raises while it is walked; each is reported as a bad file.
# ArithmeticError covers OverflowError from round() or int() of an infinite kiwi float
# (NaN raises ValueError).
_TREE_ERRORS = (KeyError, IndexError, TypeError, ValueError, AttributeError, struct.error,
                RecursionError, ArithmeticError)


class Refused(Exception):
    """An argument or input refused with exit 2."""


def die(msg: str, code: int = 2):
    print(f"fig-design: {msg}", file=sys.stderr)
    sys.exit(code)


def not_found(name: str) -> None:
    print(f"fig-design: not found: {name}", file=sys.stderr)


# ============================================================
# Locating the file and the output
# ============================================================
def design_dir(name: str) -> Path:
    try:
        d = data_path(name)  # leak-guard: ok (NAME is the operator's registry argument; the registry refuses a name it does not hold)
    except DataPathError as exc:
        raise Refused(f"{name}: {exc}") from exc
    if not d.is_dir():
        raise Refused(f"{name}: {d} is not a directory")
    return d


def latest_fig(d: Path) -> Path:
    dated = []
    for p in d.iterdir():
        m = _DATED_FIG.search(p.name)
        if m and p.is_file():
            dated.append((m.group(1), p.name, p))
    if not dated:
        listing = ", ".join(sorted(p.name for p in d.iterdir())) or "(empty)"
        raise Refused(f"no *YYYY-MM-DD.fig in {d}; it holds: {listing}")
    return max(dated)[2]


def out_dir(out: str, ds: Path) -> Path:
    o = Path(out).expanduser().resolve()
    dsr = ds.resolve()
    if o == dsr or dsr in o.parents:
        raise Refused(f"--out {o} is inside the design-system directory; choose another")
    o.mkdir(parents=True, exist_ok=True)
    return o


# ============================================================
# Subcommands
# ============================================================
def cmd_info(fig) -> int:
    ns = fig.tree.get("nodeChanges", [])
    print(f"nodes: {len(ns)}")
    print(f"pages: {sum(1 for n in ns if n.get('type') == 'CANVAS')}")
    print(f"components: {sum(1 for n in ns if n.get('type') == 'SYMBOL')}")
    print(f"variables: {sum(1 for n in ns if n.get('type') == 'VARIABLE')}")
    print(f"text styles: {sum(1 for n in ns if n.get('styleType') == 'TEXT')}")
    fonts = {(n["fontName"].get("family", ""), n["fontName"].get("style", ""))
             for n in ns if isinstance(n.get("fontName"), dict)}
    for fam, style in sorted(fonts):
        print(f"font: {fam} {style}")
    return 0


def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9.]+", "-", s).strip("-")[:80]


def split_parts(doc, n) -> list:
    parts = [n]
    rounds = 0
    while any(p["size"]["x"] > SPLIT_WIDTH for p in parts) and rounds < MAX_SPLIT_ROUNDS:
        nxt = []
        for p in parts:
            if p["size"]["x"] > SPLIT_WIDTH:
                nxt += [c for c in doc.kids.get(K(p["guid"]), [])
                        if c.get("size", {}).get("x", 0) >= SPLIT_MIN_CHILD and c.get("visible", True)]
            else:
                nxt.append(p)
        parts = nxt
        rounds += 1
    return parts


def write_svg(path: Path, svg: str) -> None:
    path.write_text(svg, encoding="utf-8")


def cmd_render(fig, ds, args) -> int:
    out = out_dir(args.out, ds)
    doc = fig_render.Doc(fig, fig_render.load_proxy_map(ds))
    css = fig_render.fonts_css_path(ds)
    missing = 0
    svgs = []
    for name in args.frame:
        hits = [n for n in doc.ns if n.get("name") == name and n.get("type") in ("FRAME", "SYMBOL")]
        if not hits:
            not_found(name)
            missing += 1
            continue
        n = max(hits, key=lambda n: n["size"]["x"] * n["size"]["y"])
        parts = split_parts(doc, n) if n["size"]["x"] > SPLIT_WIDTH else [n]
        for i, p in enumerate(parts):
            fn = slug(name) + (f'__{i + 1:02d}-{slug(p.get("name", ""))}' if p is not n else "") + ".svg"
            write_svg(out / fn, fig_render.frame_svg(doc, p))
            svgs.append(out / fn)
            print(f"wrote {fn} {round(p['size']['x'])}x{round(p['size']['y'])}")
    shoot_svgs(svgs, css, out)
    return 1 if missing else 0


def parse_spec(spec: str) -> tuple[str, str, str]:
    if "=" not in spec:
        raise Refused(f"--spec {spec!r} is not OUTNAME=FRAME[:CHILD-PREFIX]")
    outname, rest = spec.split("=", 1)
    if not _OUTNAME.fullmatch(outname):
        raise Refused(f"--spec OUTNAME {outname!r} must be letters, digits, '.', '_' or '-'")
    if ":" in rest:
        frame, child = rest.rsplit(":", 1)
    else:
        frame, child = rest, "Screen"
    return outname, frame, child


def find_child(doc, parent_name: str, child: str):
    """The first FRAME under the widest node named `parent_name` whose name starts with `child`.

    Returns (parent, child); either may be None.
    """
    cands = [n for n in doc.ns if n.get("name") == parent_name]
    if not cands:
        return None, None
    p = max(cands, key=lambda n: n.get("size", {}).get("x", 0))
    st = [p]
    seen = set()
    while st:
        n = st.pop(0)
        if id(n) in seen:
            continue
        seen.add(id(n))
        if (n.get("name") or "").startswith(child) and n.get("type") == "FRAME" and n is not p:
            return p, n
        if "guid" in n:
            st += doc.kids.get(K(n["guid"]), [])
    return p, None


def cmd_shells(fig, ds, args) -> int:
    specs = [parse_spec(s) for s in args.spec]
    out = out_dir(args.out, ds)
    doc = fig_render.Doc(fig, fig_render.load_proxy_map(ds))
    css = fig_render.fonts_css_path(ds)
    missing = 0
    svgs = []
    for outname, frame, child in specs:
        p, n = find_child(doc, frame, child)
        if p is None:
            not_found(frame)
            missing += 1
            continue
        if n is None:
            not_found(f"{frame}:{child}")
            missing += 1
            continue
        svg = fig_render.frame_svg(doc, n, skip_prefixes=args.skip_prefix, blank_slots=args.blank_slot)
        write_svg(out / f"{outname}.svg", svg)
        svgs.append(out / f"{outname}.svg")
        print(f"wrote {outname}.svg {round(n['size']['x'])}x{round(n['size']['y'])}")
    shoot_svgs(svgs, css, out)
    return 1 if missing else 0


def _flat(v):
    while isinstance(v, dict):
        v = v.get("value")
    return f"{v:g}px" if isinstance(v, (int, float)) else v


def _comment(s) -> str:
    return str(s).replace("*/", "* /")


def tokens_css(d: dict, header: str, prefix: str) -> str:
    css = [header, ":root {"]
    seen = {}

    def fallback(s):
        return f"--{prefix}-" + re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    for t in sorted(d["tokens"], key=lambda t: (t["set"] != "Semantic", t["set"] or "", t["name"])):
        mm = re.match(r"var\((--[^)]+)\)", t["web"] or "")
        name = mm.group(1) if mm else fallback(t["name"])
        if name in seen:
            name = fallback((t["set"] or "None") + "-" + t["name"])
        if name in seen:
            continue
        v = _flat(list(t["values"].values())[0]) if t["values"] else None
        if v is None or not isinstance(v, str):
            continue
        if not name.startswith("--") or _CSS_UNSAFE.search(name) or _CSS_UNSAFE.search(v):
            print(f"fig-design: token {t['set']} / {t['name']} skipped: unsafe CSS name or value",
                  file=sys.stderr)
            continue
        seen[name] = 1
        css.append(f'  {name}: {v}; /* {_comment(t["set"])} / {_comment(t["name"])} */')
    css.append("}")
    return "\n".join(css) + "\n"


def _hexc(p):
    c = p.get("color")
    if not c:
        return p.get("type", "?")
    a = c.get("a", 1) * p.get("opacity", 1)
    return "#%02x%02x%02x" % tuple(round(c[k] * 255) for k in "rgb") + ("" if a > .999 else "/%d%%" % round(a * 100))


def component_specs(fig, pages: list, fmap: dict) -> tuple[str, list]:
    ns = fig.tree.get("nodeChanges", [])
    by = {K(n["guid"]): n for n in ns}
    kids = collections.defaultdict(list)
    for n in ns:
        if n.get("parentIndex"):
            kids[K(n["parentIndex"]["guid"])].append(n)
    for v in kids.values():
        v.sort(key=lambda n: n["parentIndex"].get("position", ""))
    vars_ = {K(n["guid"]): n["name"] for n in ns if n.get("type") == "VARIABLE"}

    def colvar(p):
        cv = p.get("colorVar") or {}
        al = ((cv.get("value") or {}).get("alias") or {}).get("guid")
        return f"[{vars_.get(K(al))}]" if al else ""

    def line(n, dep):
        t = n.get("type")
        s = n.get("size", {})
        bits = [f'{t} "{n.get("name")}" {s.get("x", 0):.0f}x{s.get("y", 0):.0f}']
        if n.get("stackMode") and n["stackMode"] != "NONE":
            bits.append(f'auto-layout {n["stackMode"]} gap {n.get("stackSpacing", 0):g} '
                        f'pad {n.get("stackVerticalPadding", 0):g}/{n.get("stackHorizontalPadding", 0):g}/'
                        f'{n.get("stackPaddingBottom", n.get("stackVerticalPadding", 0)):g}/'
                        f'{n.get("stackPaddingRight", n.get("stackHorizontalPadding", 0)):g}')
        r = n.get("cornerRadius") or n.get("rectangleTopLeftCornerRadius")
        if r:
            bits.append(f"radius {r:g}")
        f = [_hexc(p) + colvar(p) for p in n.get("fillPaints", []) if p.get("visible", True)]
        if f and t != "TEXT":
            bits.append("fill " + ",".join(f))
        st = [_hexc(p) + colvar(p) for p in n.get("strokePaints", []) if p.get("visible", True)]
        if st:
            bits.append(f'stroke {n.get("strokeWeight", 1):g} ' + ",".join(st))
        if t == "TEXT":
            fn = n.get("fontName", {})
            fam, wt = fmap.get((fn.get("family"), fn.get("style")),
                               (fn.get("family"), fig_render.W.get(fn.get("style"), 400)))
            bits.append(f'text "{(n.get("textData") or {}).get("characters", "")[:60]}" {fam} {wt} '
                        f'{n.get("fontSize", 0):g}px color {",".join(f)}')
        if t == "INSTANCE":
            sym = by.get(K(n["symbolData"]["symbolID"])) if n.get("symbolData", {}).get("symbolID") else None
            bits.append(f'uses <{sym.get("name") if sym else "?"}>')
        if not n.get("visible", True):
            bits.append("HIDDEN")
        return "  " * dep + " · ".join(bits)

    page_nodes = {n["name"]: n for n in ns if n.get("type") == "CANVAS"}
    outl = []
    missing = []
    for pname in pages:
        pg = page_nodes.get(pname)
        if not pg:
            outl.append(f"## {pname}: NOT FOUND")
            missing.append(pname)
            continue
        outl.append(f"\n## {pname}")
        stack = list(kids[K(pg["guid"])])
        seen = set()
        syms = []
        while stack:
            n = stack.pop(0)
            if id(n) in seen:
                continue
            seen.add(id(n))
            if n.get("type") == "SYMBOL":
                syms.append(n)
            else:
                stack += kids.get(K(n["guid"]), [])
        for sy in syms[:40]:
            par = by.get(K(sy["parentIndex"]["guid"]))
            outl.append(f'\n### {(par.get("name") + " / ") if par and par.get("isStateGroup") else ""}{sy["name"]}')

            def walk(n, dep):
                outl.append(line(n, dep))
                if dep < 4:
                    for c in kids.get(K(n["guid"]), []):
                        walk(c, dep + 1)
            walk(sy, 0)
    return "\n".join(outl) + "\n", missing


def cmd_kit(fig, ds, args) -> int:
    if not _VAR_PREFIX.fullmatch(args.var_prefix):
        raise Refused(f"--var-prefix {args.var_prefix!r} must be letters, digits, '_' or '-'")
    out = out_dir(args.out, ds)
    fmap = fig_render.font_map(fig_render.load_proxy_map(ds))
    (out / "tokens.css").write_text(tokens_css(digest(fig), args.header, args.var_prefix), encoding="utf-8")
    specs, missing = component_specs(fig, args.pages, fmap)
    (out / "component-specs.txt").write_text(specs, encoding="utf-8")
    for p in missing:
        not_found(p)
    print(f"wrote {out / 'tokens.css'}")
    print(f"wrote {out / 'component-specs.txt'}")
    return 1 if missing else 0


def shoot_svgs(svgs: list, css: Path, out: Path) -> None:
    if not svgs:
        return
    if not css.is_file():
        print(f"fig-design: fonts CSS {css.name} not found; text falls back to system fonts",
              file=sys.stderr)
    with fig_render.chromium() as b:
        for s in svgs:
            for png in fig_render.svg_to_png(s, css, out, browser=b):
                print(f"png {png.name}")


def cmd_shot(ds, args) -> int:
    out = out_dir(args.out, ds)
    css = fig_render.fonts_css_path(ds)
    missing = 0
    files = []
    for f in args.files:
        p = Path(f)
        if not p.is_file():
            not_found(f)
            missing += 1
            continue
        if p.suffix.lower() not in (".svg", ".html", ".htm"):
            raise Refused(f"{f}: only .svg, .html or .htm can be shot")
        files.append(p)
    if files:
        with fig_render.chromium() as b:
            for p in files:
                if p.suffix.lower() == ".svg":
                    pngs = fig_render.svg_to_png(p, css, out, browser=b)
                else:
                    pngs = [fig_render.html_to_png(p, out, browser=b)]
                for png in pngs:
                    print(f"png {png}")
    return 1 if missing else 0


# ============================================================
# Entry point
# ============================================================
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="fig-design.py", description="Read and render a Figma .fig design-system file.")
    ap.add_argument("name", metavar="NAME", help="data registry name of the design-system directory")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", help="counts and fonts, one key: value per line")
    r = sub.add_parser("render", help="frames to SVG and PNG")
    r.add_argument("--frame", action="append", required=True)
    r.add_argument("--out", required=True)
    s = sub.add_parser("shells", help="empty app shells to SVG and PNG")
    s.add_argument("--spec", action="append", required=True, help="OUTNAME=FRAME or OUTNAME=FRAME:CHILD-PREFIX")
    s.add_argument("--skip-prefix", action="append", default=[])
    s.add_argument("--blank-slot", action="append", default=[])
    s.add_argument("--out", required=True)
    k = sub.add_parser("kit", help="tokens.css and component-specs.txt")
    k.add_argument("--pages", nargs="+", required=True)
    k.add_argument("--header", required=True)
    k.add_argument("--var-prefix", required=True)
    k.add_argument("--out", required=True)
    sh = sub.add_parser("shot", help="SVG or HTML files to PNG")
    sh.add_argument("--out", required=True)
    sh.add_argument("files", nargs="+", metavar="FILE")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        ds = design_dir(args.name)
        if args.cmd == "shot":
            return cmd_shot(ds, args)
        if args.cmd == "shells":
            for spec in args.spec:
                parse_spec(spec)
        if args.cmd in ("render", "shells", "kit"):
            out_dir(args.out, ds)
        fig = load(latest_fig(ds))
    except (Refused, FigFileError, ValueError, DataPathError, DataRootError) as exc:
        die(str(exc))
    except PlaywrightError as exc:
        die(f"Chromium: {str(exc).splitlines()[0] if str(exc) else type(exc).__name__}")
    except OSError as exc:
        die(f"{type(exc).__name__}: {exc}")
    try:
        with fig:
            if args.cmd == "info":
                return cmd_info(fig)
            if args.cmd == "render":
                return cmd_render(fig, ds, args)
            if args.cmd == "shells":
                return cmd_shells(fig, ds, args)
            return cmd_kit(fig, ds, args)
    except (Refused, FigFileError, DataPathError, DataRootError) as exc:
        die(str(exc))
    except PlaywrightError as exc:
        die(f"Chromium: {str(exc).splitlines()[0] if str(exc) else type(exc).__name__}")
    except _TREE_ERRORS as exc:
        die(f"{fig.path.name}: malformed document ({type(exc).__name__}: {exc})")
    except OSError as exc:
        die(f"{type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
