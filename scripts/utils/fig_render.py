"""Render frames of a decoded Figma .fig tree to SVG, and SVG or HTML to PNG.

The SVG renderer resolves instances through symbolOverrides and
derivedSymbolData, component properties (visibility, text, swapped symbols,
slot contents), clipping, image paints, drop shadows and text on the derived
baselines. It is approximate: no blend modes, masks, or gradients beyond a
vertical linear fill.

Proxy fonts are mapped to the real ones through `<design-system dir>/fonts/proxy-map.json`
(`load_proxy_map`); with no such file, nothing is mapped.

Every text run and every attribute value taken from the file is escaped with
`html.escape(..., quote=True)`. Image bytes come from `FigFile.image()`.

Public interface: `Doc`, `frame_svg`, `load_proxy_map`, `svg_to_png`,
`html_to_png`, `K`.
"""
from __future__ import annotations

import base64
import html
import json
import os
import re
import struct
import tempfile
from pathlib import Path

from scripts.utils.data_path import data_write_dir
from scripts.utils.fig_file import FigFile
from scripts.utils.user_agent import user_agent

W = {"Thin": 100, "ExtraLight": 200, "Light": 300, "Regular": 400, "Medium": 500,
     "SemiBold": 600, "Semi Bold": 600, "Bold": 700, "ExtraBold": 800, "Black": 900}
MAX_RENDER_DEPTH = 40
PAGE_TIMEOUT_MS = 60_000
TILE_THRESHOLD = 1800
TILE_HEIGHT = 1600
_CSS_FAMILY_UNSAFE = re.compile(r"[^A-Za-z0-9 _.-]")


def K(g: dict) -> tuple:
    return (g["sessionID"], g["localID"])


def _e(v) -> str:
    """An attribute value or text run taken from the file, escaped."""
    return html.escape(str(v), quote=True)


def load_proxy_map(ds_dir) -> list:
    """The `map` list of `<ds_dir>/fonts/proxy-map.json`, or [] with no file."""
    p = Path(ds_dir) / "fonts" / "proxy-map.json"
    if not p.is_file():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    entries = data.get("map", []) if isinstance(data, dict) else []
    out = []
    for e in entries:
        if (isinstance(e, dict) and isinstance(e.get("proxy_family"), str)
                and isinstance(e.get("proxy_style"), str) and isinstance(e.get("family"), str)
                and isinstance(e.get("weight"), int)):
            out.append(e)
        else:
            raise ValueError(f"{p}: malformed map entry {e!r}")
    return out


def font_map(proxy_map: list) -> dict:
    """(proxy family, proxy style) -> (family, weight)."""
    return {(e["proxy_family"], e["proxy_style"]): (e["family"], e["weight"]) for e in proxy_map}


def fonts_css_path(ds_dir) -> Path:
    """The fonts CSS for screenshots: `fonts_css` of proxy-map.json, else fonts/web/fonts.css.

    The path must stay inside `ds_dir`; one that leaves it is refused.
    """
    ds = Path(ds_dir).resolve()
    rel = "fonts/web/fonts.css"
    p = ds / "fonts" / "proxy-map.json"
    if p.is_file():
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("fonts_css"), str):
            rel = data["fonts_css"]
    out = (ds / rel).resolve()
    if out != ds and ds not in out.parents:
        raise ValueError(f"fonts_css {rel!r} leaves the design-system directory")
    return out


def norm(a: dict) -> dict:
    """A component property value, read from `value` or `varValue.value`."""
    v = dict(a.get("value") or {})
    vv = (a.get("varValue") or {}).get("value") or {}
    for k, x in vv.items():
        v.setdefault(k, x)
    sv = v.get("symbolIdValue")
    if isinstance(sv, dict) and "guid" in sv:
        v["symbolIdValue"] = sv["guid"]
    return v


# ============================================================
# SVG rendering
# ============================================================
class Doc:
    def __init__(self, fig: FigFile, proxy_map: list):
        tree = fig.tree
        self.fig = fig
        self.blobs = [base64.b64decode(b["bytes"]) for b in tree.get("blobs", [])]
        self.ns = tree.get("nodeChanges", [])
        self.by = {K(n["guid"]): n for n in self.ns if "guid" in n}
        self.kids = {}
        for n in self.ns:
            p = n.get("parentIndex")
            if p:
                self.kids.setdefault(K(p["guid"]), []).append(n)
        for v in self.kids.values():
            v.sort(key=lambda n: n["parentIndex"].get("position", ""))
        self.vars = {K(n["guid"]): n for n in self.ns if n.get("type") == "VARIABLE"}
        self.font_map = font_map(proxy_map)
        self.skip_prefixes: tuple = ()
        self.blank_slots: tuple = ()
        self.filters = {}
        self.clips = 0
        self.out = []
        self.cur_size = (0, 0)

    def path_d(self, blob) -> str:
        if not isinstance(blob, int) or not 0 <= blob < len(self.blobs):
            return ""
        b = self.blobs[blob]
        i = 0
        d = []

        def f(j):
            return struct.unpack_from("<f", b, j)[0]
        while i < len(b):
            c = b[i]
            i += 1
            need = {0: 0, 1: 8, 2: 8, 3: 16, 4: 24}.get(c)
            if need is None or i + need > len(b):
                break
            if c == 0:
                d.append("Z")
            elif c in (1, 2):
                d.append(("M" if c == 1 else "L") + "%.2f %.2f" % (f(i), f(i + 4)))
                i += 8
            elif c == 3:
                d.append("Q" + " ".join("%.2f" % f(i + 4 * k) for k in range(4)))
                i += 16
            elif c == 4:
                d.append("C" + " ".join("%.2f" % f(i + 4 * k) for k in range(6)))
                i += 24
        return "".join(d)

    def color(self, p):
        c = p.get("color", {"r": 0, "g": 0, "b": 0, "a": 1})
        return ("#%02x%02x%02x" % tuple(round(max(0, min(1, c[k])) * 255) for k in "rgb"),
                c.get("a", 1) * p.get("opacity", 1))

    def paint(self, p, d, extra=""):
        if not p.get("visible", True):
            return
        t = p.get("type")
        if t == "SOLID":
            col, a = self.color(p)
            self.out.append(f'<path d="{_e(d)}" fill="{col}" fill-opacity="{a:.3f}"{extra}/>')
        elif t and t.startswith("GRADIENT"):
            st = p.get("stops") or p.get("gradientStops") or []
            if st:
                gid = f"g{len(self.out)}"
                stops = "".join(
                    f'<stop offset="{x.get("position", 0):.3f}" stop-color="{self.color(x)[0]}" '
                    f'stop-opacity="{x["color"].get("a", 1):.3f}"/>' for x in st)
                self.out.append(f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1" '
                                f'gradientUnits="objectBoundingBox">{stops}</linearGradient></defs>')
                self.out.append(f'<path d="{_e(d)}" fill="url(#{gid})" '
                                f'fill-opacity="{p.get("opacity", 1):.3f}"{extra}/>')
        elif t == "IMAGE":
            h = p.get("image", {}).get("hash")
            if h:
                raw = self.fig.image(base64.b64decode(h).hex())
                if raw is not None:
                    self.clips += 1
                    cid = f"ic{self.clips}"
                    data = base64.b64encode(raw).decode()
                    self.out.append(f'<clipPath id="{cid}"><path d="{_e(d)}"/></clipPath>')
                    self.out.append(
                        f'<image clip-path="url(#{cid})" x="0" y="0" width="{_e(self.cur_size[0])}" '
                        f'height="{_e(self.cur_size[1])}" preserveAspectRatio="xMidYMid slice" '
                        f'href="data:image/png;base64,{data}"/>')

    def lookup(self, layers, path, field):
        for prefix, table in layers:  # outermost first
            if path[:len(prefix)] == prefix:
                v = table.get(path[len(prefix):])
                if v and field in v:
                    return v[field]
        return None

    def prop(self, props, n, field):
        for r in n.get("componentPropRefs", []) or []:
            if r.get("componentPropNodeField") == field and not r.get("isDeleted"):
                v = props.get(K(r["defID"]))
                if v:
                    return v
        return None

    def render(self, n, path, ov, der, props, depth=0):
        if depth > MAX_RENDER_DEPTH:
            return

        def g(f):
            return self.lookup(ov, path, f) if path else None

        def dv(f):
            return self.lookup(der, path, f) if path else None
        vis = g("visible")
        pv = self.prop(props, n, "VISIBLE")
        if pv is not None and "boolValue" in pv:
            vis = pv["boolValue"]
        if vis is None:
            vis = n.get("visible", True)
        if not vis:
            return
        nm = n.get("name") or ""
        if any(nm.startswith(x) for x in self.skip_prefixes):
            return
        tr = dv("transform") or g("transform") or n.get("transform") or {}
        size = dv("size") or g("size") or n.get("size") or {"x": 0, "y": 0}
        self.cur_size = (size["x"], size["y"])
        op = g("opacity")
        op = n.get("opacity", 1) if op is None else op
        m = (tr.get("m00", 1), tr.get("m10", 0), tr.get("m01", 0), tr.get("m11", 1),
             tr.get("m02", 0), tr.get("m12", 0))
        attrs = f' transform="matrix({" ".join("%.3f" % x for x in m)})"' if depth else ""
        if op < 1:
            attrs += f' opacity="{op:.3f}"'
        eff = g("effects") or n.get("effects") or []
        sh = [e for e in eff if e.get("type") == "DROP_SHADOW" and e.get("visible", True)]
        if sh:
            e = sh[0]
            col, a = self.color(e)
            key = (col, round(a, 2), e.get("offset", {}).get("x", 0), e.get("offset", {}).get("y", 0),
                   e.get("radius", 0))
            if key not in self.filters:
                self.filters[key] = f"f{len(self.filters)}"
                self.out.append(
                    f'<defs><filter id="{self.filters[key]}" x="-20%" y="-20%" width="140%" height="160%">'
                    f'<feDropShadow dx="{_e(key[2])}" dy="{_e(key[3])}" stdDeviation="{key[4] / 2:.1f}" '
                    f'flood-color="{col}" flood-opacity="{a:.2f}"/></filter></defs>')
            attrs += f' filter="url(#{self.filters[key]})"'
        self.out.append(f"<g{attrs}>")
        t = n.get("type")
        fills = g("fillPaints")
        fills = n.get("fillPaints", []) if fills is None else fills
        strokes = g("strokePaints")
        strokes = n.get("strokePaints", []) if strokes is None else strokes
        if t == "TEXT":
            self.text(n, g, dv, props, fills)
        else:
            fg = dv("fillGeometry") or n.get("fillGeometry") or []
            if (t in ("FRAME", "INSTANCE", "SYMBOL", "ROUNDED_RECTANGLE", "RECTANGLE")
                    and (dv("size") or g("size")) and not dv("fillGeometry")):
                r = g("cornerRadius") or n.get("cornerRadius", 0) or n.get("rectangleTopLeftCornerRadius", 0)
                x, y = size["x"], size["y"]
                ds = [f"M{r} 0H{x - r}Q{x} 0 {x} {r}V{y - r}Q{x} {y} {x - r} {y}H{r}Q0 {y} 0 {y - r}V{r}Q0 0 {r} 0Z"]
            else:
                ds = [self.path_d(x["commandsBlob"]) for x in fg if "commandsBlob" in x]
            for d in ds:
                for p in fills:
                    self.paint(p, d)
            sg = dv("strokeGeometry") or n.get("strokeGeometry") or []
            for x in sg:
                if "commandsBlob" in x:
                    d = self.path_d(x["commandsBlob"])
                    for p in strokes:
                        self.paint(p, d)
        # children
        if t == "INSTANCE":
            sym_id = g("overriddenSymbolID") or n.get("overriddenSymbolID") or n.get("symbolData", {}).get("symbolID")
            ps = self.prop(props, n, "OVERRIDDEN_SYMBOL_ID")
            if ps and (ps.get("guidValue") or ps.get("symbolIdValue")):
                sym_id = ps.get("guidValue") or ps.get("symbolIdValue")
            sym = self.by.get(K(sym_id)) if sym_id else None
            if sym:
                sd = n.get("symbolData", {})
                newov = {tuple(K(x) for x in o["guidPath"]["guids"]): o
                         for o in sd.get("symbolOverrides", []) if "guidPath" in o}
                newder = {tuple(K(x) for x in o["guidPath"]["guids"]): o
                          for o in n.get("derivedSymbolData", []) if "guidPath" in o}
                # instance-local assignments; outer overrides of this instance's assignments win
                np = dict(props)
                for d in sym.get("componentPropDefs", []) or []:
                    iv = norm(d)
                    if iv:
                        np.setdefault(K(d["id"]), iv)
                for a in (n.get("componentPropAssignments") or []) + (g("componentPropAssignments") or []):
                    v = norm(a)
                    if v:
                        np[K(a["defID"])] = v
                ov2 = ov + [(path, newov)] if path else [((), newov)]
                der2 = der + [(path, newder)] if path else [((), newder)]
                clipped = self.clip_open(n, size)
                for c in self.kids.get(K(sym["guid"]), []):
                    self.render(c, path + (K(c["guid"]),), ov2, der2, np, depth + 1)
                self.clip_close(clipped)
        else:
            kids = self.kids.get(K(n["guid"]), []) if "guid" in n else []
            slot = self.prop(props, n, "SLOT_CONTENT_ID")
            sid = slot and slot.get("slotContentIdValue", {}).get("guid")
            if sid and n.get("name") in self.blank_slots:
                kids = []
                sid = None
            if sid and sid.get("sessionID") != 4294967295:
                kids = self.kids.get(K(sid), [])
                clipped = self.clip_open(n, size)
                for c in kids:
                    self.render(c, (), [], [], {}, depth + 1)
                self.clip_close(clipped)
                kids = []
            if kids:
                clipped = self.clip_open(n, size)
                for c in kids:
                    cp = path[:-1] + (K(c["guid"]),) if path else ()
                    self.render(c, cp, ov, der, props, depth + 1)
                self.clip_close(clipped)
        self.out.append("</g>")

    def clip_open(self, n, size) -> bool:
        if n.get("type") in ("FRAME", "INSTANCE", "SYMBOL") and not n.get("frameMaskDisabled", False):
            self.clips += 1
            cid = f"c{self.clips}"
            self.out.append(f'<clipPath id="{cid}"><rect width="{size["x"]:.2f}" height="{size["y"]:.2f}"/>'
                            f'</clipPath><g clip-path="url(#{cid})">')
            return True
        return False

    def clip_close(self, clipped: bool):
        if clipped:
            self.out.append("</g>")

    def text(self, n, g, dv, props, fills):
        td = g("textData") or n.get("textData") or {}
        pt = self.prop(props, n, "TEXT_DATA")
        if pt and (pt.get("textValue") or pt.get("textDataValue")):
            td = pt.get("textValue") or pt.get("textDataValue")
        chars = td.get("characters", "")
        dtd = dv("derivedTextData") or (n.get("derivedTextData") if not (g("textData") or pt) else None) or {}
        fn = g("fontName") or n.get("fontName") or {"family": "Inter", "style": "Regular"}
        fam, wt = self.font_map.get((fn["family"], fn["style"]),
                                    (fn["family"], W.get(fn["style"].replace(" Italic", ""), 400)))
        fs = g("fontSize") or n.get("fontSize", 14)
        col, a = self.color(fills[0]) if fills and fills[0].get("type") == "SOLID" else ("#000000", 1)
        if fills and not fills[0].get("visible", True):
            return
        if not isinstance(fs, (int, float)) or not isinstance(wt, int):
            raise TypeError("font size or weight is not a number")
        # The family sits inside a CSS string in a style attribute; html.escape guards the
        # markup, not the CSS, so characters that could close the CSS string are dropped.
        fam = _CSS_FAMILY_UNSAFE.sub("", str(fam))
        ff = f"'{fam}', 'Inter', sans-serif" if "Mono" not in fam else "'IBM Plex Mono', monospace"
        style = f"font-family:{_e(ff)};font-weight:{_e(wt)};font-size:{_e(fs)}px"
        ls = (g("letterSpacing") or n.get("letterSpacing") or {})
        if ls.get("value"):
            v = ls["value"] * fs / 100 if ls.get("units") == "PERCENT" else ls["value"]
            style += f";letter-spacing:{v:.2f}px"
        bl = dtd.get("baselines") or []
        if bl and chars:
            for b in bl:
                seg = chars[b["firstCharacter"]:b["endCharacter"]].rstrip("\n")
                if seg:
                    self.out.append(
                        f'<text x="{b["position"]["x"]:.2f}" y="{b["position"]["y"]:.2f}" style="{style}" '
                        f'fill="{col}" fill-opacity="{a:.3f}" xml:space="preserve">{_e(seg)}</text>')
        elif chars:
            size = dv("size") or g("size") or n.get("size") or {"x": 0, "y": fs}
            al = n.get("textAlignHorizontal", "LEFT")
            anchor, x = {"CENTER": ("middle", size["x"] / 2), "RIGHT": ("end", size["x"])}.get(al, ("start", 0))
            lh = fs * 1.3
            lines = chars.split("\n")
            y0 = ((size["y"] - lh * len(lines)) / 2 + fs * 0.95
                  if n.get("textAlignVertical") == "CENTER" else fs * 0.95)
            for i, ln in enumerate(lines):
                self.out.append(
                    f'<text x="{x:.2f}" y="{y0 + i * lh:.2f}" text-anchor="{anchor}" style="{style}" '
                    f'fill="{col}" fill-opacity="{a:.3f}" xml:space="preserve">{_e(ln)}</text>')


def frame_svg(doc: Doc, node: dict, skip_prefixes=(), blank_slots=()) -> str:
    """One frame as a standalone SVG document."""
    # Every SVG is standalone: ids and definitions from an earlier frame must not leak in.
    doc.out = []
    doc.filters = {}
    doc.clips = 0
    doc.cur_size = (0, 0)
    doc.skip_prefixes = tuple(skip_prefixes)
    doc.blank_slots = tuple(blank_slots)
    sz = node["size"]
    doc.render(node, (), [], [], {}, 0)
    body = "".join(doc.out)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{sz["x"]:.0f}" height="{sz["y"]:.0f}" '
            f'viewBox="0 0 {sz["x"]:.0f} {sz["y"]:.0f}">{body}</svg>')


# ============================================================
# PNG through headless Chromium
# ============================================================
class _Chromium:
    """One headless Chromium for several shots."""

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        try:
            self.browser = self._pw.chromium.launch()
        except BaseException:
            self._pw.stop()
            raise
        return self.browser

    def __exit__(self, *exc):
        try:
            self.browser.close()
        finally:
            self._pw.stop()


def chromium() -> _Chromium:
    return _Chromium()


_SVG_SIZE_RE = re.compile(r'width="([\d.]+)" height="([\d.]+)"')


def svg_to_png(svg_path, fonts_css, out_dir, browser=None) -> list[Path]:
    """Screenshot an SVG with the fonts CSS linked; tall pages become 1600 px tiles.

    The wrap page is written under the runtime tmp directory, never beside the
    input, and deleted after the shot. Returns the PNG paths written.
    """
    if browser is None:
        with chromium() as b:
            return svg_to_png(svg_path, fonts_css, out_dir, browser=b)
    svg_path = Path(svg_path)
    out_dir = Path(out_dir)
    svg = svg_path.read_text(encoding="utf-8")
    m = _SVG_SIZE_RE.search(svg)
    if not m:
        raise ValueError(f"{svg_path.name}: no width/height attributes on the SVG")
    w, h = (float(x) for x in m.groups())
    css_href = _e(Path(fonts_css).resolve().as_uri())
    page_html = (f'<!doctype html><html><head><link rel="stylesheet" href="{css_href}">'
                 f'<style>body{{margin:0;background:#fff}}</style></head><body>{svg}</body></html>')
    fd, tmp = tempfile.mkstemp(suffix=".html", dir=str(data_write_dir("runtime:fig-design/tmp")))
    wrap = Path(tmp)
    written = []
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(page_html)
        pg = browser.new_page(user_agent=user_agent(), viewport={"width": max(int(w), 1), "height": 1000})
        try:
            pg.set_default_timeout(PAGE_TIMEOUT_MS)
            pg.goto(wrap.as_uri(), timeout=PAGE_TIMEOUT_MS)
            pg.evaluate("document.fonts.ready")
            pg.wait_for_timeout(300)
            if h <= TILE_THRESHOLD:
                out = out_dir / (svg_path.stem + ".png")
                pg.screenshot(path=str(out), full_page=True)
                written.append(out)
            else:
                y = 0
                i = 1
                while y < h:
                    out = out_dir / f"{svg_path.stem}__tile{i:02d}.png"
                    pg.screenshot(path=str(out), full_page=True,
                                  clip={"x": 0, "y": y, "width": w, "height": min(TILE_HEIGHT, h - y)})
                    written.append(out)
                    y += TILE_HEIGHT
                    i += 1
        finally:
            pg.close()
    finally:
        wrap.unlink(missing_ok=True)
    return written


def html_to_png(html_path, out_dir, browser=None) -> Path:
    """Screenshot an HTML page as it is: 1920x1080, then resized to its scroll size."""
    if browser is None:
        with chromium() as b:
            return html_to_png(html_path, out_dir, browser=b)
    html_path = Path(html_path)
    out = Path(out_dir) / (html_path.stem + ".png")
    pg = browser.new_page(user_agent=user_agent(), viewport={"width": 1920, "height": 1080})
    try:
        pg.set_default_timeout(PAGE_TIMEOUT_MS)
        pg.goto(html_path.resolve().as_uri(), timeout=PAGE_TIMEOUT_MS)
        pg.evaluate("document.fonts.ready")
        pg.wait_for_timeout(300)
        w, h = pg.evaluate("[document.body.scrollWidth, document.body.scrollHeight]")
        pg.set_viewport_size({"width": max(int(w), 200), "height": max(int(h), 100)})
        pg.screenshot(path=str(out), full_page=True)
    finally:
        pg.close()
    return out
