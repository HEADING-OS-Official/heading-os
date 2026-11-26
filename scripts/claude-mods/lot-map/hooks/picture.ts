// lot-map: the map as a picture, for a terminal that shows Kitty graphics (the new
// Herdr). The same ten stations as braille.ts, in the same order and at the same
// angles, drawn at the terminal's own resolution, about one picture pixel to one
// screen pixel, so nothing is scaled up into steps.
//
// The design follows the original lot-map's published screenshot (HOS-424), rebuilt
// from the picture alone: no line of the original's code was read or copied. Each
// station is a dark disc with a thin ring and a line icon of this module's own
// drawing, its name under it and its calls under the name; a restrained palette,
// grey at rest, amber for a station running now or on the recent route, red with
// a numbered badge for errors; faint dotted rings behind; straight thin spokes to
// a large centre disc whose amber arc is the context window, CLAUDE letter-spaced
// and the model below; thin curved links along the route between consecutive
// stations. Every shape is anti-aliased by coverage (a pixel takes the share of it
// a shape covers). The background stays transparent, so the map sits on the
// window's own background (HOS-421).
//
// Text is the module's own stroke font, drawn as curves, proportioned like a
// terminal's monospace face and one cell wide a character; a character it does not
// draw falls back to the 5x7 dot font below. Labels cannot use the terminal's own
// font: the engine's `Image` is a leaf whose cells are text, with no layer beneath
// text, and names no cell size in pixels, so words in cells could not be aligned
// with nodes placed in pixels (its types, read 2026-11-22).
//
// No dependency, no file, no process, no network: plain arithmetic into a pixel
// array, encoded as a PNG by the module's own deflate (fixed Huffman codes), base64
// by hand. The last picture is kept, so a redraw with the same board, running set,
// route, fill, model and size costs nothing.

import type { Board, Station } from '../types'
import { NAMES, STATIONS } from './stations'
import { roundHalfEven } from './braille'

export type Picture = { png: string }

// The pixels of one terminal cell. ASSUMED: the engine's API names no cell size
// (its types, read 2026-11-22, carry pixels only for a pointer's position), so the
// picture is drawn for a cell of about 10 x 20 pixels; the terminal scales it to
// the box, so a larger cell is drawn slightly soft, never stepped.
export const CELL_W = 10
export const CELL_H = 20

type RGB = readonly [number, number, number]

// The palette, measured off the reference screenshot 2026-11-22 (most common
// pixel of each element), the idle ring and icon a step lighter so a one-pixel
// line still reads at this module's smaller size.
const DISC: RGB = [18, 21, 28]
const RING: RGB = [54, 60, 72]
const ICON_IDLE: RGB = [88, 94, 104]
const ICON: RGB = [150, 156, 166]
const BRIGHT: RGB = [235, 230, 220]
const MID: RGB = [155, 161, 171]
const DIM: RGB = [107, 114, 128]
const AMBER: RGB = [240, 197, 149]
const AMBER_DEEP: RGB = [225, 172, 110]
const RED: RGB = [232, 101, 79]
const TEAL: RGB = [98, 199, 186]
const LINK: RGB = [150, 152, 156]
// The context arc, light grey as in the original's demo page (HOS-425).
const ARC: RGB = [178, 182, 190]
const WHITE: RGB = [255, 255, 255]

const mix = (a: RGB, b: RGB, t: number): RGB => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t]

const clamp01 = (v: number): number => (v < 0 ? 0 : v > 1 ? 1 : v)

// The distance from (px, py) to the segment (x0, y0)-(x1, y1).
const segDist = (px: number, py: number, x0: number, y0: number, x1: number, y1: number): number => {
  const dx = x1 - x0
  const dy = y1 - y0
  const len2 = dx * dx + dy * dy
  const t = len2 > 0 ? clamp01(((px - x0) * dx + (py - y0) * dy) / len2) : 0
  return Math.hypot(px - x0 - t * dx, py - y0 - t * dy)
}

// ============================================================
// Strokes
// ============================================================

// One straight piece of a stroke and its half-width in pixels; a dot when both
// ends meet.
type Seg = [x0: number, y0: number, x1: number, y1: number, r: number]

const DEG = Math.PI / 180

// Collects a drawing's strokes, in pixels, for one Canvas.stroke: lines, arcs of
// an ellipse (angles in degrees, 0 to the right, 90 down) and quadratic curves,
// each flattened into short straight pieces.
class Pen {
  readonly segs: Seg[] = []
  private x = NaN
  private y = NaN
  readonly r: number

  constructor(r: number) {
    this.r = r
  }

  move(x: number, y: number): this {
    this.x = x
    this.y = y
    return this
  }

  line(x: number, y: number): this {
    if (!Number.isNaN(this.x)) this.segs.push([this.x, this.y, x, y, this.r])
    return this.move(x, y)
  }

  // Continues from the pen when it stands at the arc's start; starts there otherwise.
  arc(cx: number, cy: number, rx: number, ry: number, a0: number, a1: number): this {
    const sx = cx + rx * Math.cos(a0 * DEG)
    const sy = cy + ry * Math.sin(a0 * DEG)
    if (Number.isNaN(this.x) || Math.hypot(this.x - sx, this.y - sy) > 0.01) this.move(sx, sy)
    const n = Math.max(2, Math.ceil(Math.abs(a1 - a0) / 10 * Math.max(1, Math.max(rx, ry) / 12)))
    for (let k = 1; k <= n; k++) {
      const a = (a0 + (a1 - a0) * k / n) * DEG
      this.line(cx + rx * Math.cos(a), cy + ry * Math.sin(a))
    }
    return this
  }

  ellipse(cx: number, cy: number, rx: number, ry: number): this {
    this.arc(cx, cy, rx, ry, 0, 360)
    this.x = NaN
    return this
  }

  quad(qx: number, qy: number, x: number, y: number): this {
    const x0 = this.x
    const y0 = this.y
    const n = Math.max(4, Math.ceil(Math.hypot(x - x0, y - y0) / 3))
    for (let k = 1; k <= n; k++) {
      const t = k / n
      const s = 1 - t
      this.line(s * s * x0 + 2 * s * t * qx + t * t * x, s * s * y0 + 2 * s * t * qy + t * t * y)
    }
    return this
  }

  poly(points: readonly (readonly [number, number])[], closed = false): this {
    points.forEach(([x, y], i) => (i === 0 ? this.move(x, y) : this.line(x, y)))
    if (closed && points[0]) this.line(points[0][0], points[0][1])
    this.x = NaN
    return this
  }

  dot(x: number, y: number, r = this.r * 1.35): this {
    this.segs.push([x, y, x, y, r])
    this.x = NaN
    return this
  }
}

// ============================================================
// The font
// ============================================================

// The stroke font. A glyph sits in a cell 12 units wide: its ink between x 2 and
// 10, capitals and digits from y 0 to the baseline at 14, lowercase from 4,
// descenders to 18. Commands: M x,y move; L x,y line; Q qx,qy,x,y curve;
// A cx,cy,rx,ry,a0,a1 arc (degrees, 0 right, 90 down, swept from a0 to a1);
// E cx,cy,rx,ry ellipse; D x,y dot. Drawn by this module for HOS-424.
const STROKES: Record<string, string> = {
  A: 'M2,14 L6,0 L10,14 M3.4,9.5 L8.6,9.5',
  B: 'M2.5,7 L2.5,0 L6.4,0 A6.4,3.5,3.3,3.5,-90,90 L2.5,7 L2.5,14 L6.8,14 A6.8,10.5,3.2,3.5,90,-90 L6.4,7',
  C: 'A6.3,7,4,7,-42,-318',
  D: 'M2.5,0 L2.5,14 L5.6,14 A5.6,7,4.4,7,90,-90 L2.5,0',
  E: 'M9.8,0 L2.5,0 L2.5,14 L9.8,14 M2.5,7 L8.5,7',
  F: 'M9.8,0 L2.5,0 L2.5,14 M2.5,7 L8.5,7',
  G: 'A6.2,7,4,7,-42,-360 L6.6,7',
  H: 'M2.5,0 L2.5,14 M9.5,0 L9.5,14 M2.5,7 L9.5,7',
  I: 'M3.5,0 L8.5,0 M6,0 L6,14 M3.5,14 L8.5,14',
  J: 'M4.5,0 L9,0 M8,0 L8,10.3 A5.3,10.3,2.7,3.7,0,180',
  K: 'M2.5,0 L2.5,14 M9.8,0 L2.5,8.6 M5,5.8 L10,14',
  L: 'M2.5,0 L2.5,14 L9.8,14',
  M: 'M2,14 L2,0 L6,9.5 L10,0 L10,14',
  N: 'M2.5,14 L2.5,0 L9.5,14 L9.5,0',
  O: 'E6,7,4,7',
  P: 'M2.5,14 L2.5,0 L6.4,0 A6.4,3.8,3.5,3.8,-90,90 L2.5,7.6',
  Q: 'E6,7,4,7 M7,10.2 L10.2,15',
  R: 'M2.5,14 L2.5,0 L6.4,0 A6.4,3.8,3.5,3.8,-90,90 L2.5,7.6 M6.2,7.6 L10,14',
  S: 'A6,3.6,3.6,3.6,-25,-270 A6,10.6,3.9,3.4,-90,155',
  T: 'M2,0 L10,0 M6,0 L6,14',
  U: 'M2.5,0 L2.5,9.6 A6,9.6,3.5,4.4,180,0 L9.5,0',
  V: 'M2,0 L6,14 L10,0',
  W: 'M1.5,0 L3.5,14 L6,5.5 L8.5,14 L10.5,0',
  X: 'M2.5,0 L9.5,14 M9.5,0 L2.5,14',
  Y: 'M2,0 L6,7 L10,0 M6,7 L6,14',
  Z: 'M2.5,0 L9.5,0 L2.5,14 L9.5,14',
  a: 'E5.8,9,3.4,5 M9.2,4 L9.2,14',
  b: 'M2.8,0 L2.8,14 E6.2,9,3.4,5',
  c: 'A6.2,9,3.6,5,-42,-318',
  d: 'E5.8,9,3.4,5 M9.2,0 L9.2,14',
  e: 'M2.6,9 L9.6,9 A6.1,9,3.5,5,0,-308',
  f: 'M6,14 L6,3.2 A8.4,3.2,2.4,2.8,180,310 M3,4.5 L9,4.5',
  g: 'E5.8,9,3.4,5 M9.2,4 L9.2,15 A5.9,15,3.3,3,0,155',
  h: 'M2.8,0 L2.8,14 M2.8,8 A6.1,8,3.3,4,180,360 L9.4,14',
  i: 'M3.5,4 L6,4 L6,14 M3.5,14 L8.5,14 D6,1',
  j: 'M4,4 L7.5,4 L7.5,15.3 A5,15.3,2.5,2.7,0,155 D7.5,1',
  k: 'M3,0 L3,14 M9.2,4 L3,10.2 M5.4,8 L9.6,14',
  l: 'M3.5,0 L6,0 L6,14 M3.5,14 L8.5,14',
  m: 'M2,14 L2,4 M2,7 A4,7,2,3,180,360 L6,14 M6,7 A8,7,2,3,180,360 L10,14',
  n: 'M2.8,14 L2.8,4 M2.8,8 A6.1,8,3.3,4,180,360 L9.4,14',
  o: 'E6,9,3.6,5',
  p: 'M2.8,4 L2.8,18 E6.2,9,3.4,5',
  q: 'E5.8,9,3.4,5 M9.2,4 L9.2,18',
  r: 'M3.5,4 L3.5,14 M3.5,8.6 A7,8.6,3.5,4.6,180,295',
  s: 'A6,6.6,3.4,2.6,-25,-270 A6,11.6,3.6,2.4,-90,155',
  t: 'M5,1.5 L5,11.6 A7.4,11.6,2.4,2.4,180,65 M2.5,4.5 L9.5,4.5',
  u: 'M2.8,4 L2.8,10 A6.1,10,3.3,4,180,0 M9.4,4 L9.4,14',
  v: 'M2.5,4 L6,14 L9.5,4',
  w: 'M1.5,4 L3.7,14 L6,7 L8.3,14 L10.5,4',
  x: 'M2.5,4 L9.5,14 M9.5,4 L2.5,14',
  y: 'M2.5,4 L6,14 M9.5,4 L5.2,16.3 Q4.4,18,2.8,18',
  z: 'M2.8,4 L9.2,4 L2.8,14 L9.2,14',
  0: 'E6,7,3.8,7 D6,7',
  1: 'M3.5,2.8 L6.5,0 L6.5,14 M3.5,14 L9.5,14',
  2: 'A6,3.8,3.6,3.8,-165,15 L2.5,14 L9.8,14',
  3: 'A6,3.5,3.5,3.5,-155,90 A6,10.5,3.8,3.5,-90,155',
  4: 'M8,14 L8,0 L2,10 L10.5,10',
  5: 'M9.5,0 L3,0 L2.6,6.2 Q4.2,5.2,6,5.2 A6,9.6,3.8,4.4,-90,155',
  6: 'A6,7,3.8,7,-55,-180 L2.2,9.8 E6,9.8,3.8,4.2',
  7: 'M2.5,0 L9.8,0 L4.5,14',
  8: 'E6,3.6,3.3,3.6 E6,10.6,3.8,3.4',
  9: 'E6,4.2,3.7,4.2 M9.7,4.2 L9.7,7 A6,7,3.7,7,0,125',
  '.': 'D6,13.3',
  ',': 'M6.4,12.6 L5,16.5',
  ':': 'D6,5 D6,13.3',
  '%': 'E3.6,3,1.7,2.4 E8.4,11,1.7,2.4 M9.5,0.5 L2.5,13.5',
  '+': 'M6,4 L6,12 M2,8 L10,8',
  '-': 'M3,8 L9,8',
  '!': 'M6,0 L6,9.5 D6,13.3',
  '?': 'A6,3.6,3.6,3.6,-170,90 L6,9.5 D6,13.3',
  '/': 'M9.5,-0.5 L2.5,15',
  '~': 'M2.5,8.5 Q4,6,6,8 Q8,10,9.5,7.5',
  '↓': 'M6,1.5 L6,13.2 M2.8,9.8 L6,13.2 L9.2,9.8',
}

type Cmd = [op: string, n: number[]]

const PARSED = new Map<string, Cmd[]>()

const commandsOf = (ch: string): Cmd[] | undefined => {
  const cached = PARSED.get(ch)
  if (cached) return cached
  const def = STROKES[ch]
  if (def === undefined) return undefined
  const cmds = def.split(/\s+/).map((t): Cmd => [t[0]!, t.slice(1).split(',').map(Number)])
  PARSED.set(ch, cmds)
  return cmds
}

// A size of text: `u` pixels a font unit (a character is 12 units wide), `sw`
// the stroke's width in pixels, `track` extra pixels between two characters.
type Font = { u: number; sw: number; track?: number }

const advanceOf = (f: Font): number => 12 * f.u + (f.track ?? 0)
const textWidth = (s: string, f: Font): number => Math.max(0, Array.from(s).length * advanceOf(f) - (f.track ?? 0))
// Capitals and digits; the line a label takes is a little more.
const capHeight = (f: Font): number => 14 * f.u

// One character's strokes into `pen`, its cell's top-left corner at (x, y).
const glyph = (pen: Pen, ch: string, x: number, y: number, u: number): void => {
  if (ch === ' ') return
  const cmds = commandsOf(ch)
  const X = (v: number): number => x + v * u
  const Y = (v: number): number => y + v * u
  if (!cmds) {
    // The 5x7 dot font, stretched over the stroke font's capital box.
    for (const [c0, r0, c1, r1] of strokesOf(ch.codePointAt(0)!)) {
      pen.move(X(2 + c0 * 2), Y(r0 * 14 / 6)).line(X(2 + c1 * 2), Y(r1 * 14 / 6))
    }
    return
  }
  for (const [op, n] of cmds) {
    if (op === 'M') pen.move(X(n[0]!), Y(n[1]!))
    else if (op === 'L') pen.line(X(n[0]!), Y(n[1]!))
    else if (op === 'Q') pen.quad(X(n[0]!), Y(n[1]!), X(n[2]!), Y(n[3]!))
    else if (op === 'A') pen.arc(X(n[0]!), Y(n[1]!), n[2]! * u, n[3]! * u, n[4]!, n[5]!)
    else if (op === 'E') pen.ellipse(X(n[0]!), Y(n[1]!), n[2]! * u, n[3]! * u)
    else if (op === 'D') pen.dot(X(n[0]!), Y(n[1]!))
  }
}

// The 5x7 dot font, the fallback for a character the stroke font does not draw.
// The 5x7 font, printable ASCII from space to `~`: 7 rows a glyph, each row one
// character of 0-9a-v (5 bits, bit 4 the leftmost pixel).
const FONT =
  '00000004444404aaa0000aavavaa4fke5u4op248j3cik8lid448000024888428422248' +
  '04lel40044v4400000c48000v00000000cc01248g0ehjlphe4c4444eeh1248vv2421he' +
  '26aiv22vgu11he68guhhev124888ehhehheehhf12c0cc0cc00cc0c48248g84200v0v00' +
  '8421248eh12404eh1dlleehhvhhhuhhuhhuehggghesihhhisvgguggvvggugggehgnhhf' +
  'hhhvhhhe44444e72222ichikokihggggggvhrllhhhhhpljhhehhhhheuhhugggehhhlid' +
  'uhhukihfgge11uv444444hhhhhhehhhhha4hhhlllahha4ahhhhha444v1248gve88888e' +
  '0g84210e22222e4ah0000000000v842000000e1fhfggmphhu00egghe11djhhf00ehvge' +
  '698s8880fhhf1eggmphhh40c444e20622icggikokic44444e00qllhh00mphhh00ehhhe' +
  '00uhugg00djf1100mpggg00ege1u88s889600hhhjd00hhha400hhlla00ha4ah00hhf1e' +
  '00v248v244844244444448442448008l200'

const GLYPH_W = 5
const GLYPH_H = 7
const ADVANCE = GLYPH_W + 1

type Cell = [c0: number, r0: number, c1: number, r1: number]

const GLYPHS = new Map<number, Cell[]>()

// A glyph's strokes: each dot joins its neighbour to the right and below, and a
// diagonal neighbour only where no square corner already joins the two, so the
// dots of the bitmap become one smooth line.
const strokesOf = (code: number): Cell[] => {
  const at = (code >= 32 && code <= 126 ? code : 63) - 32
  const cached = GLYPHS.get(at)
  if (cached) return cached
  const bits = Array.from({ length: GLYPH_H }, (_, row) => parseInt(FONT[at * GLYPH_H + row]!, 32))
  const lit = (c: number, r: number): boolean =>
    c >= 0 && c < GLYPH_W && r >= 0 && r < GLYPH_H && (bits[r]! & (1 << (GLYPH_W - 1 - c))) !== 0
  const out: Cell[] = []
  for (let r = 0; r < GLYPH_H; r++) {
    for (let c = 0; c < GLYPH_W; c++) {
      if (!lit(c, r)) continue
      out.push([c, r, c, r])
      if (lit(c + 1, r)) out.push([c, r, c + 1, r])
      if (lit(c, r + 1)) out.push([c, r, c, r + 1])
      if (lit(c + 1, r + 1) && !lit(c + 1, r) && !lit(c, r + 1)) out.push([c, r, c + 1, r + 1])
      if (lit(c - 1, r + 1) && !lit(c - 1, r) && !lit(c, r + 1)) out.push([c, r, c - 1, r + 1])
    }
  }
  GLYPHS.set(at, out)
  return out
}

// ============================================================
// The canvas
// ============================================================

type Run = { text: string; c: RGB }

class Canvas {
  readonly w: number
  readonly h: number
  // Premultiplied colour, 0 to 255, and alpha, 0 to 1.
  readonly p: Float32Array

  // `p`, when given, is the canvas's starting pixels (taken, not copied).
  constructor(w: number, h: number, p?: Float32Array) {
    this.w = w
    this.h = h
    this.p = p ?? new Float32Array(w * h * 4)
  }

  // Lays colour `c` at opacity `a` over pixel (x, y).
  blend(x: number, y: number, c: RGB, a: number): void {
    if (a <= 0) return
    const i = (y * this.w + x) * 4
    const p = this.p
    const q = 1 - a
    p[i] = c[0] * a + p[i]! * q
    p[i + 1] = c[1] * a + p[i + 1]! * q
    p[i + 2] = c[2] * a + p[i + 2]! * q
    p[i + 3] = a + p[i + 3]! * q
  }

  // Calls `f` with every pixel's centre in the box, clipped to the canvas.
  each(x0: number, y0: number, x1: number, y1: number, f: (x: number, y: number, px: number, py: number) => void): void {
    const xa = Math.max(0, Math.floor(x0))
    const ya = Math.max(0, Math.floor(y0))
    const xb = Math.min(this.w - 1, Math.ceil(x1))
    const yb = Math.min(this.h - 1, Math.ceil(y1))
    for (let y = ya; y <= yb; y++) for (let x = xa; x <= xb; x++) f(x, y, x + 0.5, y + 0.5)
  }

  // A filled disc.
  disc(cx: number, cy: number, r: number, c: RGB, a = 1): void {
    this.each(cx - r - 1, cy - r - 1, cx + r + 1, cy + r + 1,
      (x, y, px, py) => this.blend(x, y, c, a * clamp01(r + 0.5 - Math.hypot(px - cx, py - cy))))
  }

  // A circle `width` pixels wide whose middle runs at radius `r`.
  ring(cx: number, cy: number, r: number, width: number, c: RGB, a = 1): void {
    const o = r + width / 2 + 1
    this.each(cx - o, cy - o, cx + o, cy + o,
      (x, y, px, py) => this.blend(x, y, c, a * clamp01(width / 2 + 0.5 - Math.abs(Math.hypot(px - cx, py - cy) - r))))
  }

  // A soft halo from radius `r0` out, fading to nothing at `r1`.
  glow(cx: number, cy: number, r0: number, r1: number, c: RGB, a: number): void {
    this.each(cx - r1, cy - r1, cx + r1, cy + r1, (x, y, px, py) => {
      const d = Math.hypot(px - cx, py - cy)
      if (d >= r1) return
      const t = d <= r0 ? 1 : 1 - (d - r0) / (r1 - r0)
      this.blend(x, y, c, a * t * t)
    })
  }

  // A flat band between radii `r0` and `r1`, edges anti-aliased.
  band(cx: number, cy: number, r0: number, r1: number, c: RGB, a: number): void {
    this.each(cx - r1 - 1, cy - r1 - 1, cx + r1 + 1, cy + r1 + 1, (x, y, px, py) => {
      const d = Math.hypot(px - cx, py - cy)
      this.blend(x, y, c, a * clamp01(d - r0 + 0.5) * clamp01(r1 - d + 0.5))
    })
  }

  // A soft band from radius `r0` out, fading to nothing at `r1`, nothing inside `r0`.
  halo(cx: number, cy: number, r0: number, r1: number, c: RGB, a: number): void {
    this.each(cx - r1, cy - r1, cx + r1, cy + r1, (x, y, px, py) => {
      const d = Math.hypot(px - cx, py - cy)
      if (d >= r1 || d < r0 - 1) return
      const t = d < r0 ? d - (r0 - 1) : 1 - (d - r0) / (r1 - r0)
      this.blend(x, y, c, a * t * t)
    })
  }

  // Strokes as one coverage mask, so a joint where two pieces meet is never laid
  // on twice, then the mask in one colour, nothing inside the `holes`.
  stroke(segs: readonly Seg[], c: RGB, a = 1, holes: readonly Rect[] = []): void {
    if (segs.length === 0 || a <= 0) return
    let xa = Infinity
    let ya = Infinity
    let xb = -Infinity
    let yb = -Infinity
    for (const [x0, y0, x1, y1, r] of segs) {
      xa = Math.min(xa, x0 - r, x1 - r)
      ya = Math.min(ya, y0 - r, y1 - r)
      xb = Math.max(xb, x0 + r, x1 + r)
      yb = Math.max(yb, y0 + r, y1 + r)
    }
    const ox = Math.max(0, Math.floor(xa) - 1)
    const oy = Math.max(0, Math.floor(ya) - 1)
    const mw = Math.min(this.w, Math.ceil(xb) + 2) - ox
    const mh = Math.min(this.h, Math.ceil(yb) + 2) - oy
    if (mw <= 0 || mh <= 0) return
    const mask = new Float32Array(mw * mh)
    for (const [x0, y0, x1, y1, r] of segs) {
      const sx = Math.max(0, Math.floor(Math.min(x0, x1) - r - 1) - ox)
      const sy = Math.max(0, Math.floor(Math.min(y0, y1) - r - 1) - oy)
      const ex = Math.min(mw - 1, Math.ceil(Math.max(x0, x1) + r + 1) - ox)
      const ey = Math.min(mh - 1, Math.ceil(Math.max(y0, y1) + r + 1) - oy)
      for (let my = sy; my <= ey; my++) {
        for (let mx = sx; mx <= ex; mx++) {
          const cov = clamp01(r + 0.5 - segDist(ox + mx + 0.5, oy + my + 0.5, x0, y0, x1, y1))
          const i = my * mw + mx
          if (cov > mask[i]!) mask[i] = cov
        }
      }
    }
    for (let my = 0; my < mh; my++) {
      for (let mx = 0; mx < mw; mx++) {
        const v = mask[my * mw + mx]!
        if (v <= 0) continue
        const px = ox + mx + 0.5
        const py = oy + my + 0.5
        if (holes.some(h => px > h.x && px < h.x + h.w && py > h.y && py < h.y + h.h)) continue
        this.blend(ox + mx, oy + my, c, a * v)
      }
    }
  }

  // Text from its cell's top-left corner, run by run in their colours.
  text(x: number, y: number, runs: readonly Run[], f: Font, a = 1): void {
    const adv = advanceOf(f)
    let col = 0
    for (const run of runs) {
      const pen = new Pen(f.sw / 2)
      for (const ch of Array.from(run.text)) {
        pen.move(NaN, NaN)
        glyph(pen, ch, x + col * adv, y, f.u)
        col += 1
      }
      this.stroke(pen.segs, run.c, a)
    }
  }

  // Straight RGBA bytes.
  rgba(): Uint8Array {
    const out = new Uint8Array(this.w * this.h * 4)
    const p = this.p
    for (let i = 0; i < out.length; i += 4) {
      const a = p[i + 3]!
      if (a <= 0) continue
      out[i] = Math.min(255, Math.round(p[i]! / a))
      out[i + 1] = Math.min(255, Math.round(p[i + 1]! / a))
      out[i + 2] = Math.min(255, Math.round(p[i + 2]! / a))
      out[i + 3] = Math.round(Math.min(1, a) * 255)
    }
    return out
  }
}

// ============================================================
// The icons
// ============================================================

// A station's line icon, centred on (x, y), about 2s pixels across, in `c`.
const icon = (cv: Canvas, st: Station, x: number, y: number, s: number, sw: number, c: RGB): void => {
  const pen = new Pen(sw / 2)
  const P = (u: number, v: number): [number, number] => [x + u * s, y + v * s]
  const pts = (list: [number, number][]): [number, number][] => list.map(([u, v]) => P(u, v))
  switch (st) {
    case 'you': // a person: head and shoulders
      pen.ellipse(x, y - 0.36 * s, 0.3 * s, 0.3 * s)
      pen.arc(x, y + 0.78 * s, 0.62 * s, 0.6 * s, 192, 348)
      break
    case 'model': { // a chip: body, core and pins
      const b = 0.46
      pen.poly(pts([[-b, -b], [b, -b], [b, b], [-b, b]]), true)
      pen.poly(pts([[-0.18, -0.18], [0.18, -0.18], [0.18, 0.18], [-0.18, 0.18]]), true)
      for (const k of [-0.22, 0, 0.22]) {
        pen.poly(pts([[k, -b], [k, -0.7]])).poly(pts([[k, b], [k, 0.7]]))
        pen.poly(pts([[-b, k], [-0.7, k]])).poly(pts([[b, k], [0.7, k]]))
      }
      break
    }
    case 'hooks': // a hook: eye, shank, bend and point
      pen.ellipse(x + 0.12 * s, y - 0.66 * s, 0.14 * s, 0.14 * s)
      pen.move(...P(0.12, -0.52)).line(...P(0.12, 0.28))
      pen.arc(x - 0.2 * s, y + 0.28 * s, 0.32 * s, 0.38 * s, 0, 180)
      pen.line(...P(-0.52, -0.02)).line(...P(-0.34, 0.14))
      break
    case 'skills': // lightning
      pen.poly(pts([[0.16, -0.76], [-0.42, 0.1], [-0.02, 0.1], [-0.16, 0.76], [0.42, -0.1], [0.02, -0.1]]), true)
      break
    case 'memory': { // three linked nodes
      const n: [number, number][] = [[0, -0.5], [-0.56, 0.44], [0.56, 0.44]]
      const r = 0.17
      for (const [u, v] of n) pen.ellipse(x + u * s, y + v * s, r * s, r * s)
      for (const [i, j] of [[0, 1], [1, 2], [2, 0]] as const) {
        const [u0, v0] = n[i]!
        const [u1, v1] = n[j]!
        const d = Math.hypot(u1 - u0, v1 - v0)
        const t = r / d
        pen.poly(pts([[u0 + (u1 - u0) * t, v0 + (v1 - v0) * t], [u1 - (u1 - u0) * t, v1 - (v1 - v0) * t]]))
      }
      break
    }
    case 'agents': { // one branching into two
      const r = 0.16
      pen.ellipse(x, y - 0.52 * s, r * s, r * s)
      pen.ellipse(x - 0.5 * s, y + 0.52 * s, r * s, r * s)
      pen.ellipse(x + 0.5 * s, y + 0.52 * s, r * s, r * s)
      pen.poly(pts([[0, -0.36], [0, 0.02]]))
      pen.poly(pts([[-0.4, 0.38], [0, 0.02], [0.4, 0.38]]))
      break
    }
    case 'files': // a page with a folded corner
      pen.poly(pts([[-0.42, -0.62], [0.14, -0.62], [0.42, -0.32], [0.42, 0.62], [-0.42, 0.62]]), true)
      pen.poly(pts([[0.14, -0.62], [0.14, -0.32], [0.42, -0.32]]))
      break
    case 'shell': // a prompt
      pen.poly(pts([[-0.52, -0.34], [-0.14, 0], [-0.52, 0.34]]))
      pen.poly(pts([[0.04, 0.4], [0.52, 0.4]]))
      break
    case 'network': // a globe
      pen.ellipse(x, y, 0.62 * s, 0.62 * s)
      pen.ellipse(x, y, 0.26 * s, 0.62 * s)
      pen.poly(pts([[-0.62, 0], [0.62, 0]]))
      break
    case 'browser': // a window
      pen.poly(pts([[-0.64, -0.5], [0.64, -0.5], [0.64, 0.5], [-0.64, 0.5]]), true)
      pen.poly(pts([[-0.64, -0.22], [0.64, -0.22]]))
      pen.dot(x - 0.48 * s, y - 0.36 * s, sw * 0.55).dot(x - 0.32 * s, y - 0.36 * s, sw * 0.55)
      break
  }
  cv.stroke(pen.segs, c)
}


// ============================================================
// PNG: zlib deflate with fixed Huffman codes, CRC-32, base64
// ============================================================

const LEN_BASE = [3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 17, 19, 23, 27, 31, 35, 43, 51, 59, 67, 83, 99, 115, 131, 163, 195, 227, 258]
const LEN_EXTRA = [0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 0]
const DIST_BASE = [1, 2, 3, 4, 5, 7, 9, 13, 17, 25, 33, 49, 65, 97, 129, 193, 257, 385, 513, 769, 1025, 1537, 2049,
  3073, 4097, 6145, 8193, 12289, 16385, 24577]
const DIST_EXTRA = [0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12, 13, 13]

const LEN_CODE = new Uint8Array(259)
for (let c = 0; c < LEN_BASE.length; c++) {
  const top = c + 1 < LEN_BASE.length ? LEN_BASE[c + 1]! : 259
  for (let n = LEN_BASE[c]!; n < top && n <= 258; n++) LEN_CODE[n] = c
}
LEN_CODE[258] = 28

const distCode = (d: number): number => {
  let c = DIST_BASE.length - 1
  while (DIST_BASE[c]! > d) c--
  return c
}

class BitWriter {
  buf = new Uint8Array(1 << 16)
  n = 0
  acc = 0
  bits = 0

  private byte(v: number): void {
    if (this.n === this.buf.length) {
      const next = new Uint8Array(this.buf.length * 2)
      next.set(this.buf)
      this.buf = next
    }
    this.buf[this.n++] = v
  }

  // `len` bits of `v`, least significant first, as deflate packs them.
  put(v: number, len: number): void {
    this.acc |= v << this.bits
    this.bits += len
    while (this.bits >= 8) {
      this.byte(this.acc & 255)
      this.acc >>>= 8
      this.bits -= 8
    }
  }

  // A Huffman code, which deflate packs most significant bit first.
  code(v: number, len: number): void {
    let r = 0
    for (let i = 0; i < len; i++) r |= ((v >> i) & 1) << (len - 1 - i)
    this.put(r, len)
  }

  done(): Uint8Array {
    if (this.bits > 0) this.byte(this.acc & 255)
    this.acc = 0
    this.bits = 0
    return this.buf.subarray(0, this.n)
  }
}

const literal = (w: BitWriter, v: number): void => {
  if (v < 144) w.code(0x30 + v, 8)
  else if (v < 256) w.code(0x190 + v - 144, 9)
  else if (v < 280) w.code(v - 256, 7)
  else w.code(0xc0 + v - 280, 8)
}

// One final block of fixed Huffman codes over LZ77 matches found by a hash chain.
export const deflate = (d: Uint8Array): Uint8Array => {
  const w = new BitWriter()
  w.put(1, 1) // BFINAL
  w.put(1, 2) // BTYPE 01: fixed Huffman codes
  const WSIZE = 32768
  const HSIZE = 1 << 15
  const head = new Int32Array(HSIZE).fill(-1)
  const prev = new Int32Array(WSIZE).fill(-1)
  const n = d.length
  const hash = (i: number): number => ((d[i]! << 10) ^ (d[i + 1]! << 5) ^ d[i + 2]!) & (HSIZE - 1)
  const insert = (i: number): void => {
    if (i + 2 >= n) return
    const h = hash(i)
    prev[i & (WSIZE - 1)] = head[h]!
    head[h] = i
  }
  let i = 0
  while (i < n) {
    let best = 0
    let bestDist = 0
    if (i + 2 < n) {
      const max = Math.min(258, n - i)
      let j = head[hash(i)]!
      for (let chain = 0; j >= 0 && i - j <= WSIZE && chain < 32; chain++) {
        if (d[j + best] === d[i + best]) {
          let len = 0
          while (len < max && d[j + len] === d[i + len]) len++
          if (len > best) {
            best = len
            bestDist = i - j
            if (len === max) break
          }
        }
        const next = prev[j & (WSIZE - 1)]!
        if (next >= j) break
        j = next
      }
    }
    if (best >= 3) {
      const lc = LEN_CODE[best]!
      literal(w, 257 + lc)
      if (LEN_EXTRA[lc]) w.put(best - LEN_BASE[lc]!, LEN_EXTRA[lc]!)
      const dc = distCode(bestDist)
      w.code(dc, 5)
      if (DIST_EXTRA[dc]) w.put(bestDist - DIST_BASE[dc]!, DIST_EXTRA[dc]!)
      for (let k = 0; k < best; k++) insert(i + k)
      i += best
    } else {
      literal(w, d[i]!)
      insert(i)
      i += 1
    }
  }
  literal(w, 256) // end of block
  return w.done()
}

const CRC_TABLE = new Uint32Array(256)
for (let n = 0; n < 256; n++) {
  let c = n
  for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
  CRC_TABLE[n] = c >>> 0
}

const crc32 = (bytes: Uint8Array): number => {
  let c = 0xffffffff
  for (let i = 0; i < bytes.length; i++) c = CRC_TABLE[(c ^ bytes[i]!) & 255]! ^ (c >>> 8)
  return (c ^ 0xffffffff) >>> 0
}

const adler32 = (bytes: Uint8Array): number => {
  let a = 1
  let b = 0
  for (let i = 0; i < bytes.length;) {
    const end = Math.min(bytes.length, i + 5552)
    for (; i < end; i++) {
      a += bytes[i]!
      b += a
    }
    a %= 65521
    b %= 65521
  }
  return ((b << 16) | a) >>> 0
}

const be32 = (v: number): number[] => [(v >>> 24) & 255, (v >>> 16) & 255, (v >>> 8) & 255, v & 255]

// RGBA pixels as a whole PNG file: every row filtered Sub, one IDAT.
export const encodePng = (rgba: Uint8Array, width: number, height: number): Uint8Array => {
  const stride = width * 4
  const raw = new Uint8Array(height * (stride + 1))
  for (let y = 0; y < height; y++) {
    const o = y * (stride + 1)
    raw[o] = 1 // Sub
    for (let x = 0; x < stride; x++) {
      const v = rgba[y * stride + x]!
      raw[o + 1 + x] = x >= 4 ? (v - rgba[y * stride + x - 4]!) & 255 : v
    }
  }
  const body = deflate(raw)
  const zlib = new Uint8Array(body.length + 6)
  zlib[0] = 0x78
  zlib[1] = 0x01
  zlib.set(body, 2)
  zlib.set(be32(adler32(raw)), body.length + 2)
  const chunk = (type: string, data: Uint8Array): Uint8Array => {
    const out = new Uint8Array(data.length + 12)
    out.set(be32(data.length), 0)
    for (let k = 0; k < 4; k++) out[4 + k] = type.charCodeAt(k)
    out.set(data, 8)
    out.set(be32(crc32(out.subarray(4, 8 + data.length))), 8 + data.length)
    return out
  }
  const ihdr = new Uint8Array([...be32(width), ...be32(height), 8, 6, 0, 0, 0])
  const parts = [new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]), chunk('IHDR', ihdr), chunk('IDAT', zlib),
    chunk('IEND', new Uint8Array(0))]
  const out = new Uint8Array(parts.reduce((s, p) => s + p.length, 0))
  let at = 0
  for (const p of parts) {
    out.set(p, at)
    at += p.length
  }
  return out
}

const B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'

const B64_CODES = Uint8Array.from(B64, ch => ch.charCodeAt(0))

// Base64 by hand, written as character codes and turned into a string in
// chunks: a frame's raw pixels run to hundreds of kilobytes.
export const toBase64 = (bytes: Uint8Array): string => {
  const n = bytes.length
  const out = new Uint8Array(Math.ceil(n / 3) * 4)
  let o = 0
  let i = 0
  for (; i + 2 < n; i += 3) {
    const v = (bytes[i]! << 16) | (bytes[i + 1]! << 8) | bytes[i + 2]!
    out[o++] = B64_CODES[(v >> 18) & 63]!
    out[o++] = B64_CODES[(v >> 12) & 63]!
    out[o++] = B64_CODES[(v >> 6) & 63]!
    out[o++] = B64_CODES[v & 63]!
  }
  if (i < n) {
    const v = (bytes[i]! << 16) | (i + 1 < n ? bytes[i + 1]! << 8 : 0)
    out[o++] = B64_CODES[(v >> 18) & 63]!
    out[o++] = B64_CODES[(v >> 12) & 63]!
    out[o++] = i + 1 < n ? B64_CODES[(v >> 6) & 63]! : 61
    out[o++] = 61
  }
  const parts: string[] = []
  for (let k = 0; k < out.length; k += 8192) parts.push(String.fromCharCode(...out.subarray(k, k + 8192)))
  return parts.join('')
}

// ============================================================
// The map
// ============================================================

type Rect = { x: number; y: number; w: number; h: number }

const overlaps = (a: Rect, b: Rect): boolean => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h

// How many of the route's newest stations count as recently busy.
const RECENT = 4

let last: { key: string; picture: Picture } | undefined

// The map as a PNG: `cols` x `rows` cells of `cellW` x `cellH` pixels. `running`
// holds the stations a call runs at now; `route` the main loop's latest stations,
// oldest first; `fill` the context window's share in use, 0 to 1.
export const renderPicture = (b: Board, running: ReadonlySet<Station>, route: readonly Station[], fill: number,
  model: string, cols = 76, rows = 21, cellW = CELL_W, cellH = CELL_H): Picture => {
  const key = JSON.stringify([STATIONS.map(st => [b[st].calls, b[st].errors, b[st].hookMemory]),
    STATIONS.filter(st => running.has(st)), route, fill, model, cols, rows, cellW, cellH])
  if (last && last.key === key) return last.picture
  const { width, height, rgba } = drawMap(b, running, route, fill, model, cols, rows, cellW, cellH)
  const picture = { png: toBase64(encodePng(rgba, width, height)) }
  last = { key, picture }
  return picture
}

// The map's pixels, before encoding.
export const drawMap = (b: Board, running: ReadonlySet<Station>, route: readonly Station[], fill: number,
  model: string, cols: number, rows: number, cellW = CELL_W, cellH = CELL_H): { width: number; height: number; rgba: Uint8Array } => {
  const { cv } = paintMap(b, running, route, fill, model, cols, rows, cellW, cellH, false)
  return { width: cv.w, height: cv.h, rgba: cv.rgba() }
}

// A spoke a call runs along now: from the centre disc's edge (x0, y0) to the
// station's (x1, y1); the station's node at (nx, ny), radius r; red after errors.
type Spoke = { x0: number; y0: number; x1: number; y1: number; nx: number; ny: number; r: number; c: RGB }

// What a frame draws besides the spokes: the centre disc, and the route's newest
// link, which draws itself in from its older station with a light head.
type Extras = { cx: number; cy: number; R: number; link?: { segs: Seg[]; holes: Rect[]; key: string } }

// The map drawn once. `moving` leaves out what the frames draw over it: the
// beads on busy spokes and the outer ring round a busy station.
const paintMap = (b: Board, running: ReadonlySet<Station>, route: readonly Station[], fill: number,
  model: string, cols: number, rows: number, cellW: number, cellH: number, moving: boolean): { cv: Canvas; spokes: Spoke[]; extras: Extras } => {
  const spokes: Spoke[] = []
  let link: Extras['link']
  const width = cols * cellW
  const height = rows * cellH
  const cv = new Canvas(width, height)

  // Labels in the terminal's own proportions: one cell wide a character.
  const u = cellW / 12
  const nameFont: Font = { u, sw: Math.max(1.15, u * 1.55) }
  const countFont: Font = { u: u * 0.92, sw: Math.max(1, u * 1.3) }
  const line = capHeight(nameFont)
  const countGap = line * 0.55

  // Geometry: the stations on an ellipse at braille.ts's angles, each node with
  // its name and count beneath it inside the picture; the centre disc as large as
  // the stations above and below it leave room for.
  const r = Math.max(8, Math.min(26, height * 0.058, width * 0.033))
  const under = Math.max(2, r * 0.18)
  const labelH = under + line + countGap + capHeight(countFont) + 2
  const widest = Math.max(...STATIONS.map(st => textWidth(NAMES[st], nameFont)))
  const ry = Math.max(r * 2, (height - 5 - 2 * r - labelH) / 2)
  // Nearly a circle, as the original draws it (its demo page and its terminal shot):
  // a wide box leaves its sides empty rather than stretching the ring.
  const rx = Math.max(r * 2, Math.min(width * 0.4, width / 2 - 3 - Math.max(r, widest / 2), ry * 1.15))
  const cx = width / 2
  const cy = 3 + r + ry
  const R = Math.max(r * 1.4, Math.min(ry * 0.42, ry - r - labelH - 4, rx * 0.42))

  // How busy a station is: 1 while a call runs there, less the further back on the
  // route it was, 0 when it is neither.
  const recency = new Map<Station, number>()
  ;[...route].reverse().forEach((st, k) => { if (!recency.has(st) && k < RECENT) recency.set(st, k) })
  const heat = (st: Station): number => {
    if (running.has(st)) return 1
    const k = recency.get(st)
    return k === undefined ? 0 : Math.max(0.3, 0.8 - 0.17 * k)
  }

  const nodes = STATIONS.map((st, i) => {
    const a = 2 * Math.PI * i / STATIONS.length - Math.PI / 2
    return { st, a, x: cx + rx * Math.cos(a), y: cy + ry * Math.sin(a), t: b[st], heat: heat(st) }
  })
  const at = new Map(nodes.map(n => [n.st, n]))

  // Labels under each node: the name, then the calls (and Memory's arrivals in
  // teal), centred. When the two lines would run into another node or label,
  // one line, the name and the calls; failing that, the name alone. Planned here,
  // drawn last; the links and spokes leave a margin round each.
  const labels: [x: number, y: number, runs: Run[], f: Font][] = []
  const taken: Rect[] = [
    { x: cx - R, y: cy - R, w: 2 * R, h: 2 * R },
    ...nodes.map(n => ({ x: n.x - r - 1, y: n.y - r - 1, w: 2 * r + 2, h: 2 * r + 2 })),
  ]
  for (const n of nodes) {
    const used = n.t.calls > 0
    const nameColor = n.heat > 0 ? BRIGHT : MID
    const count: Run[] = used ? [{ text: String(n.t.calls), c: DIM }] : []
    if (used && n.st === 'memory' && n.t.hookMemory > 0) count.push({ text: ` ↓${n.t.hookMemory}`, c: TEAL })
    const countText = count.map(x => x.text).join('')
    const top = n.y + r + under
    const clampX = (w: number): number => Math.max(1, Math.min(width - w - 1, n.x - w / 2))
    const own = (rect: Rect): boolean => rect.x < n.x + r && rect.x + rect.w > n.x - r && rect.y < n.y + r && rect.y + rect.h > n.y - r
    const clear = (rect: Rect): boolean => rect.y + rect.h <= height
      && !taken.some(t2 => !own(t2) && overlaps(t2, rect))
    const nw = textWidth(NAMES[n.st], nameFont)
    const cw = textWidth(countText, countFont)
    const twoLines: Rect = { x: Math.min(clampX(nw), clampX(cw)), y: top, w: Math.max(nw, cw), h: line + countGap + capHeight(countFont) }
    if (count.length === 0 || clear(twoLines)) {
      labels.push([clampX(nw), top, [{ text: NAMES[n.st], c: nameColor }], nameFont])
      if (count.length) labels.push([clampX(cw), top + line + countGap, count, countFont])
      taken.push(count.length ? twoLines : { x: clampX(nw), y: top, w: nw, h: line })
      continue
    }
    const one: Run[] = [{ text: `${NAMES[n.st]} `, c: nameColor }, ...count]
    const ow = textWidth(one.map(x => x.text).join(''), nameFont)
    const oneLine: Rect = { x: clampX(ow), y: top, w: ow, h: line }
    if (clear(oneLine)) {
      labels.push([oneLine.x, top, one, nameFont])
      taken.push(oneLine)
    } else {
      labels.push([clampX(nw), top, [{ text: NAMES[n.st], c: nameColor }], nameFont])
      taken.push({ x: clampX(nw), y: top, w: nw, h: line })
    }
  }

  const holes: Rect[] = taken.slice(nodes.length + 1).map(t2 => ({ x: t2.x - 3, y: t2.y - 3, w: t2.w + 6, h: t2.h + 6 }))

  // Faint dotted rings behind everything.
  for (const k of [0.6, 0.86]) {
    const ex = rx * k
    const ey = ry * k
    const steps = Math.max(24, Math.round(2 * Math.PI * Math.sqrt((ex * ex + ey * ey) / 2) / 7))
    const pen = new Pen(0.7)
    for (let s = 0; s < steps; s++) {
      const a = 2 * Math.PI * s / steps
      pen.dot(cx + ex * Math.cos(a), cy + ey * Math.sin(a), 0.7)
    }
    cv.stroke(pen.segs, LINK, 0.22)
  }

  // The route: a thin curve between each two consecutive stations, the newest
  // the brightest, bowed so its middle passes outside the centre disc.
  for (let i = 1; i < route.length; i++) {
    const p = at.get(route[i - 1]!)
    const q = at.get(route[i]!)
    if (!p || !q || p === q) continue
    const mx = (p.x + q.x) / 2
    const my = (p.y + q.y) / 2
    let ux = mx - cx
    let uy = my - cy
    const m = Math.hypot(ux, uy)
    if (m < 1) {
      const d = Math.hypot(q.x - p.x, q.y - p.y) || 1
      ux = -(q.y - p.y) / d
      uy = (q.x - p.x) / d
    } else {
      ux /= m
      uy /= m
    }
    const d = Math.max(m * 0.7, 2 * (R + 10) - m)
    const pen = new Pen(0.6).move(p.x, p.y).quad(cx + ux * d, cy + uy * d, q.x, q.y)
    if (moving && i === route.length - 1) link = { segs: pen.segs, holes, key: route.join(' ') }
    else cv.stroke(pen.segs, LINK, Math.max(0.18, 0.6 - 0.1 * (route.length - 1 - i)), holes)
  }

  // Spokes: from the centre disc's edge to each node's, amber while busy, red
  // when that busy station had errors, grey otherwise.
  for (const n of nodes) {
    const dx = n.x - cx
    const dy = n.y - cy
    const len = Math.hypot(dx, dy) || 1
    const ux = dx / len
    const uy = dy / len
    const x0 = cx + ux * (R + 2)
    const y0 = cy + uy * (R + 2)
    const x1 = n.x - ux * (r + 2)
    const y1 = n.y - uy * (r + 2)
    const hot = n.t.errors > 0 ? RED : AMBER_DEEP
    if (n.heat > 0) {
      const pen = new Pen(n.heat >= 1 ? 1 : 0.75).move(x0, y0).line(x1, y1)
      cv.stroke(pen.segs, hot, 0.35 + 0.6 * n.heat, holes)
      if (n.heat >= 1) spokes.push({ x0, y0, x1, y1, nx: n.x, ny: n.y, r, c: n.t.errors > 0 ? RED : AMBER })
      if (n.heat >= 1 && !moving) {
        const beads = new Pen(0).dot(x0 + ux * 6, y0 + uy * 6, 2.4).dot(x1 - ux * 4, y1 - uy * 4, 2)
        cv.stroke(beads.segs, n.t.errors > 0 ? RED : AMBER)
      }
    } else {
      cv.stroke(new Pen(0.55).move(x0, y0).line(x1, y1).segs, LINK, n.t.calls ? 0.3 : 0.13, holes)
    }
  }

  // Halos: an amber band round a busy or recently used station, red when it had errors;
  // at rest an error shows in the icon and the badge only, as in the original.
  for (const n of nodes) {
    if (n.heat > 0) cv.band(n.x, n.y, r, r * 1.42, n.t.errors > 0 ? RED : AMBER_DEEP, 0.1 + 0.22 * n.heat)
  }

  // The centre: a disc with a grey rim, the context arc in amber (red from 90%),
  // and an inner amber ring.
  const tone: RGB = fill >= 0.9 ? RED : ARC
  cv.glow(cx, cy, R, R * 1.22, AMBER_DEEP, 0.1)
  cv.disc(cx, cy, R, DISC)
  cv.ring(cx, cy, R - 0.6, 1.2, RING)
  const Ra = R * 0.9
  const t = Math.max(2, R * 0.05)
  cv.ring(cx, cy, Ra, t, AMBER_DEEP, 0.2)
  if (fill > 0) {
    const pen = new Pen(t / 2 + 0.15)
    if (fill >= 1) pen.ellipse(cx, cy, Ra, Ra)
    else pen.arc(cx, cy, Ra, Ra, -90, -90 + 360 * fill)
    cv.stroke(pen.segs, tone)
  }
  const Ri = R * 0.78
  cv.ring(cx, cy, Ri, Math.max(1.2, R * 0.025), AMBER)

  // The stations: a dark disc, a ring grey at rest, amber while busy, red while
  // busy after errors; an outer ring while a call runs there; the icon.
  const iconWidth = Math.max(1.15, r * 0.07)
  for (const n of nodes) {
    const ring = n.heat > 0 ? mix(RING, n.t.errors > 0 ? RED : AMBER, 0.35 + 0.65 * n.heat) : RING
    cv.disc(n.x, n.y, r, DISC)
    cv.ring(n.x, n.y, r - 0.7, n.heat > 0 ? Math.max(1.4, r * 0.08) : 1.2, ring)
    if (n.heat >= 1 && !moving) cv.ring(n.x, n.y, r * 1.3, 1, n.t.errors > 0 ? RED : AMBER, 0.5)
    const c = n.st === 'memory' && n.t.calls ? TEAL
      : n.heat > 0 ? mix(ICON, n.t.errors > 0 ? RED : AMBER, n.heat)
        : n.t.errors > 0 ? RED : n.t.calls ? ICON : ICON_IDLE
    icon(cv, n.st, n.x, n.y, r * 0.5, iconWidth, c)
  }

  // Error badges: a red disc at the node's upper left with the count.
  for (const n of nodes) {
    if (!n.t.errors) continue
    const text = n.t.errors > 99 ? '99+' : String(n.t.errors)
    const br = Math.max(5.5, r * 0.32) * (text.length > 1 ? 1.2 : 1)
    const bx = n.x - r * 0.7
    const by = n.y - r * 0.7
    cv.disc(bx, by, br, RED)
    const f: Font = { u: Math.min(u * 0.85, (br * 1.15) / 14, (br * 1.6) / (12 * text.length)), sw: 0 }
    f.sw = Math.max(1, f.u * 1.7)
    cv.text(bx - textWidth(text, f) / 2, by - capHeight(f) / 2, [{ text, c: WHITE }], f)
  }

  // The labels, planned before anything is drawn so the lines can stop short of them.
  for (const [x, y, runs, f] of labels) cv.text(x, y, runs, f)

  // The centre's words: the fill small, CLAUDE letter-spaced, the model in amber;
  // the block shrinks until each line's corners are inside the inner ring, and
  // drops the model line when even that is not enough.
  const inner = Ri - 3
  const pct = `${roundHalfEven(100 * fill)}%`
  const place = (withModel: boolean): { lines: [string, Font, RGB][]; gap: number; top: number } | undefined => {
    for (let k = 1; k > 0.3; k *= 0.92) {
      const big: Font = { u: u * 1.45 * k, sw: Math.max(1.2, u * 1.45 * k * 1.5), track: u * 1.45 * k * 4.5 }
      const small: Font = { u: u * 0.8 * k, sw: Math.max(1, u * 0.8 * k * 1.4) }
      const mid: Font = { u: u * 0.95 * k, sw: Math.max(1.05, u * 0.95 * k * 1.45) }
      const lines: [string, Font, RGB][] = [[pct, small, DIM], ['CLAUDE', big, BRIGHT]]
      if (withModel) lines.push([model, mid, AMBER])
      const gap = capHeight(mid) * 0.75
      const blockH = lines.reduce((h, [, f]) => h + capHeight(f), 0) + gap * (lines.length - 1)
      let y = cy - blockH / 2
      let fits = true
      for (const [text, f] of lines) {
        const half = textWidth(text, f) / 2
        const far = Math.max(Math.abs(y - cy), Math.abs(y + capHeight(f) - cy))
        if (Math.hypot(half, far) > inner) fits = false
        y += capHeight(f) + gap
      }
      if (fits) return { lines, gap, top: cy - blockH / 2 }
    }
    return undefined
  }
  const words = place(model.length > 0) ?? place(false)
  if (words) {
    let y = words.top
    for (const [text, f, c] of words.lines) {
      cv.text(cx - textWidth(text, f) / 2, y, [{ text, c }], f)
      y += capHeight(f) + words.gap
    }
  }
  return { cv, spokes, extras: { cx, cy, R, link } }
}

// ============================================================
// Motion
// ============================================================

// The still part of a moving map, drawn once per board, running set, route,
// fill, model and size, and the spokes the frames animate.
export type Scene = { width: number; height: number; base: Float32Array; spokes: readonly Spoke[]; extras: Extras }

let lastScene: { key: string; scene: Scene } | undefined

export const sceneFor = (b: Board, running: ReadonlySet<Station>, route: readonly Station[], fill: number,
  model: string, cols = 76, rows = 21, cellW = CELL_W, cellH = CELL_H): Scene => {
  const key = JSON.stringify([STATIONS.map(st => [b[st].calls, b[st].errors, b[st].hookMemory]),
    STATIONS.filter(st => running.has(st)), route, fill, model, cols, rows, cellW, cellH])
  if (lastScene && lastScene.key === key) return lastScene.scene
  const { cv, spokes, extras } = paintMap(b, running, route, fill, model, cols, rows, cellW, cellH, true)
  const scene = { width: cv.w, height: cv.h, base: cv.p, spokes, extras }
  lastScene = { key, scene }
  return scene
}

// The motion's timings, in seconds, read off the original's demo captures (0.15 s
// apart, 2026-11-23): a bead reaches the station in about half a second; the
// station's ring spreads and the centre's halo breathes about once a second.
const TRIP = 1.4
const BEAT = 1.1
const SPIN = 1.6
const DRAW_IN = 0.45

// When the newest link started drawing itself in, by the route it belongs to.
let linkBorn: { key: string; t: number } | undefined

// One frame of the scene at `t` seconds. The centre's amber halo breathes; on each
// busy spoke a bead with two trailing dots runs out to the station in the first
// half of a trip and a paler one comes back in the second; round the station a
// ring spreads and fades and a short arc spins; the route's newest link draws
// itself in with a light head. Only these are drawn; the rest is the scene's
// pixels, copied.
export const drawFrame = (scene: Scene, t: number): Uint8Array => {
  const cv = new Canvas(scene.width, scene.height, scene.base.slice())
  const { cx, cy, R, link } = scene.extras
  const breath = 0.5 + 0.5 * Math.sin((2 * Math.PI * t) / BEAT)
  cv.band(cx, cy, R, R * (1.03 + 0.17 * breath), AMBER_DEEP, 0.06 + 0.28 * breath)

  if (link && link.segs.length) {
    if (!linkBorn || linkBorn.key !== link.key) linkBorn = { key: link.key, t }
    const grown = Math.min(1, Math.max(0, (t - linkBorn.t) / DRAW_IN))
    const n = Math.max(1, Math.ceil(link.segs.length * grown))
    const drawn = link.segs.slice(0, n)
    cv.stroke(drawn, LINK, 0.6, link.holes)
    const head = drawn[drawn.length - 1]!
    cv.disc(head[2], head[3], 2, BRIGHT, 0.9)
  }

  for (const s of scene.spokes) {
    const spread = (t % BEAT) / BEAT
    cv.ring(s.nx, s.ny, s.r * (1.12 + 0.6 * spread), 1.2, s.c, 0.6 * (1 - spread))
    const a0 = ((t / SPIN) % 1) * 360 - 90
    cv.stroke(new Pen(1).arc(s.nx, s.ny, s.r + 2.5, s.r + 2.5, a0, a0 + 38).segs, s.c, 0.9)
    const dx = s.x1 - s.x0
    const dy = s.y1 - s.y0
    const len = Math.hypot(dx, dy)
    if (len < 4) continue
    const at = (u: number): [number, number] => [s.x0 + dx * u, s.y0 + dy * u]
    const u = (t % TRIP) / TRIP
    if (u < 0.5) {
      const v = u / 0.5
      const gap = Math.min(0.08, 6 / len)
      for (const [j, rad, al] of [[2, 1.2, 0.45], [1, 1.6, 0.7]] as const) {
        if (v - j * gap < 0) continue
        const [tx, ty] = at(v - j * gap)
        cv.disc(tx, ty, rad, s.c, al)
      }
      const [bx, by] = at(v)
      cv.halo(bx, by, 1.5, 6, s.c, 0.3)
      cv.disc(bx, by, 2.8, mix(s.c, WHITE, 0.2))
    } else {
      const [rx, ry] = at(1 - (u - 0.5) / 0.5)
      cv.disc(rx, ry, 1.8, MID, 0.8)
    }
  }
  return cv.rgba()
}
