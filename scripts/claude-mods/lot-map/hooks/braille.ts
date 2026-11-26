// lot-map: the braille map of the stations, drawn above the rows when the pane has room.
//
// A line-for-line port of _DOT, Canvas, _k and render_map in scripts/lot-map.py,
// the same geometry and label rules; a rule changed here is changed there too.
// Python's round() and f"{x:.0f}" round half to even, so every one of them goes
// through roundHalfEven; int() stays truncation toward zero. The map is
// monochrome: a cell carries a tone, never a station colour.
//
// `layout` holds the geometry alone (station positions, spokes, labels), so a
// later renderer that draws a picture instead of braille uses the same map.

import type { Board, Station } from '../types'
import { NAMES, STATIONS } from './stations'

export type Tone = 'normal' | 'dim' | 'error' | 'warning' | 'bold'
export type Run = { text: string; tone: Tone }

// Python's round(x) and f"{x:.0f}": the nearest integer, a tie to the even one.
export const roundHalfEven = (x: number): number => {
  const f = Math.floor(x)
  const d = x - f
  if (d > 0.5) return f + 1
  if (d < 0.5) return f
  return f % 2 === 0 ? f : f + 1
}

// _k of lot-map.py, ported as its own function, as the HOS-418 brief asked.
export const kPy = (n: number | undefined, shared = false): string => {
  if (!n) return ''
  const v = n >= 1000 ? `${roundHalfEven(n / 1000)}k` : String(n)
  return `${shared ? '~' : ''}+${v}`
}

const len = (s: string): number => Array.from(s).length

// ============================================================
// Geometry
// ============================================================

export type Spoke = [x0: number, y0: number, x1: number, y1: number]

export type Place = {
  station: Station
  /** The station's angle; 0 is to the right, the first station sits at the top. */
  a: number
  /** The station's centre in dots. */
  sx: number
  sy: number
  calls: number
  errors: number
  /** One spoke, two for a well-worn one; none when unused. */
  spokes: Spoke[]
  /** The label's cell, already clamped into the map, and its text. */
  label: { col: number; row: number; text: string }
}

export type Layout = {
  /** The canvas in dots: two per cell across, four down. */
  W: number
  H: number
  cx: number
  cy: number
  places: Place[]
}

export const layout = (b: Board, cols: number, rows: number): Layout => {
  const W = cols * 2
  const H = rows * 4
  const cx = W / 2
  const cy = H / 2
  const rx = W * 0.36
  const ry = H * 0.40
  const total = STATIONS.reduce((n, st) => n + b[st].calls, 0) || 1
  const places = STATIONS.map((st, i): Place => {
    const a = 2 * Math.PI * i / STATIONS.length - Math.PI / 2
    const sx = cx + rx * Math.cos(a)
    const sy = cy + ry * Math.sin(a)
    const used = b[st].calls
    const spokes: Spoke[] = []
    if (used) {
      spokes.push([cx + 7 * Math.cos(a), cy + 7 * Math.sin(a), sx, sy])
      if (used / total > 0.15) { // a well-worn spoke gets a second line
        const ox = -Math.sin(a)
        const oy = Math.cos(a)
        spokes.push([cx + 7 * Math.cos(a) + ox, cy + 7 * Math.sin(a) + oy, sx + ox, sy + oy])
      }
    }
    const extra: string[] = []
    if (used) extra.push(String(used))
    if (b[st].errors) extra.push(`!${b[st].errors}`)
    if (b[st].ctx) extra.push(kPy(b[st].ctx))
    const text = NAMES[st] + (extra.length ? ` ${extra.join(' ')}` : '')
    let col = Math.trunc(sx / 2)
    let row = Math.trunc(sy / 4)
    if (Math.cos(a) < -0.2) col = col - len(text) - 2
    else if (Math.cos(a) > 0.2) col = col + 2
    else col = col - Math.floor(len(text) / 2)
    row = Math.sin(a) < -0.5 ? row - 1 : (Math.sin(a) > 0.5 ? row + 1 : row)
    return {
      station: st, a, sx, sy, calls: used, errors: b[st].errors, spokes,
      label: { col: Math.max(0, Math.min(cols - len(text), col)), row, text },
    }
  })
  return { W, H, cx, cy, places }
}

// ============================================================
// The braille canvas
// ============================================================

// The bit of each dot in a braille cell, by (x % 2, y % 4).
const DOT: Record<string, number> = {
  '0,0': 0x01, '0,1': 0x02, '0,2': 0x04, '1,0': 0x08, '1,1': 0x10,
  '1,2': 0x20, '0,3': 0x40, '1,3': 0x80,
}

const mod = (a: number, b: number): number => ((a % b) + b) % b

class Canvas {
  readonly cols: number
  readonly rows: number
  readonly dots: number[][]
  readonly tone: Tone[][]
  readonly text = new Map<string, [string, Tone]>()

  constructor(cols: number, rows: number) {
    this.cols = cols
    this.rows = rows
    this.dots = Array.from({ length: rows }, () => new Array<number>(cols).fill(0))
    this.tone = Array.from({ length: rows }, () => new Array<Tone>(cols).fill('normal'))
  }

  dot(x: number, y: number, tone: Tone): void {
    const cx = Math.floor(x / 2)
    const cy = Math.floor(y / 4)
    if (cx >= 0 && cx < this.cols && cy >= 0 && cy < this.rows) {
      this.dots[cy]![cx]! |= DOT[`${mod(x, 2)},${mod(y, 4)}`]!
      this.tone[cy]![cx] = tone
    }
  }

  line(x0: number, y0: number, x1: number, y1: number, tone: Tone): void {
    const steps = Math.trunc(Math.max(Math.abs(x1 - x0), Math.abs(y1 - y0))) || 1
    for (let i = 0; i <= steps; i++) {
      this.dot(roundHalfEven(x0 + (x1 - x0) * i / steps), roundHalfEven(y0 + (y1 - y0) * i / steps), tone)
    }
  }

  write(col: number, row: number, text: string, tone: Tone): void {
    Array.from(text).forEach((ch, i) => {
      if (col + i >= 0 && col + i < this.cols && row >= 0 && row < this.rows) {
        this.text.set(`${col + i},${row}`, [ch, tone])
      }
    })
  }

  // One array of runs per line: trailing blanks trimmed, adjacent cells of one tone merged.
  lines(): Run[][] {
    const out: Run[][] = []
    for (let y = 0; y < this.rows; y++) {
      const cells: [string, Tone][] = []
      for (let x = 0; x < this.cols; x++) {
        const t = this.text.get(`${x},${y}`)
        if (t) cells.push(t)
        else if (this.dots[y]![x]) cells.push([String.fromCharCode(0x2800 + this.dots[y]![x]!), this.tone[y]![x]!])
        else cells.push([' ', 'normal'])
      }
      while (cells.length && /\s/.test(cells[cells.length - 1]![0])) cells.pop()
      const runs: Run[] = []
      for (const [ch, tone] of cells) {
        const last = runs[runs.length - 1]
        if (last && last.tone === tone) last.text += ch
        else runs.push({ text: ch, tone })
      }
      out.push(runs)
    }
    return out
  }
}

// ============================================================
// The map
// ============================================================

// `running`: the stations with a call running; `fill`: the share of the context
// window in use, 0 to 1; `model`: the short model name.
export const renderBraille = (b: Board, running: ReadonlySet<Station>, fill: number, model: string,
  cols = 76, rows = 21): Run[][] => {
  const c = new Canvas(cols, rows)
  const { cx, cy, places } = layout(b, cols, rows)
  for (const p of places) {
    const tone: Tone = p.calls ? (p.errors ? 'error' : 'normal') : 'dim'
    for (const [x0, y0, x1, y1] of p.spokes) c.line(x0, y0, x1, y1, 'normal')
    // the station: an empty ring unused, a ring with a dot used, a full one at work
    for (let k = 0; k < 16; k++) {
      const t = 2 * Math.PI * k / 16
      c.dot(roundHalfEven(p.sx + 3 * Math.cos(t)), roundHalfEven(p.sy + 2 * Math.sin(t)), tone)
    }
    if (p.calls) c.dot(roundHalfEven(p.sx), roundHalfEven(p.sy), tone)
    if (running.has(p.station)) {
      for (const dx of [-1, 0, 1]) {
        for (const dy of [-1, 0, 1]) c.dot(roundHalfEven(p.sx + dx), roundHalfEven(p.sy + dy), 'normal')
      }
    }
    c.write(p.label.col, p.label.row, p.label.text, p.calls ? 'normal' : 'dim')
  }
  // the centre: its outline is the context window, filling clockwise from the top
  const centre: Tone = fill >= 0.9 ? 'error' : fill >= 0.7 ? 'warning' : 'normal'
  for (let k = 0; k < 40; k++) {
    const t = 2 * Math.PI * k / 40 - Math.PI / 2
    c.dot(roundHalfEven(cx + 6 * Math.cos(t)), roundHalfEven(cy + 4.5 * Math.sin(t)), k / 40 < fill ? centre : 'dim')
  }
  const pct = `${roundHalfEven(100 * fill)}%`
  c.write(Math.trunc(cx / 2) - Math.floor(len(pct) / 2), Math.trunc(cy / 4) - 1, pct, centre)
  const core = 'CLAUDE'
  c.write(Math.trunc(cx / 2) - Math.floor(len(core) / 2), Math.trunc(cy / 4), core, 'bold')
  c.write(Math.trunc(cx / 2) - Math.floor(len(model) / 2), Math.trunc(cy / 4) + 1, model, 'dim')
  return c.lines()
}
