// context-bar layout: the pure functions that turn the context figures into the
// three rows of the band and the panel's header. No JSX and no import, so node
// runs this file by stripping its types and a proof script can drive it as-is.

// ============================================================
// Inputs and outputs
// ============================================================

// One `used` row of the breakdown: its name, its tokens and its theme colour.
export type Row = { name: string; tokens: number; color: string }

// One turn's figures: the used total and the Messages tokens.
export type Turn = { total: number; messages: number }

// One styled run of text; a row of the band is a list of runs.
export type Run = { text: string; color?: string; bold?: boolean; dim?: boolean }

export type Thresholds = { soft: number; hard: number }

export type BandInput = {
  rows: Row[]
  window: number
  // Claude Code's own count of the context (the status line's figure), when
  // known; the blocks stay the per-category estimate.
  total?: number
  thresholds: Thresholds
  autoCompact?: number
  history: Turn[]
  columns: number
}

export const HISTORY_MAX = 5

// The handoff menu's two points, percent of the window (scripts/utils/checkpoint_autonomy.py).
export const SOFT_DEFAULT = 45
export const HARD_DEFAULT = 55

// The whole window the bar is drawn against, fixed at 1M (operator decision
// 2026-11-08: the bar shows the full window, even while this is hard-coded).
export const FULL_WINDOW = 1_000_000

const SHADOW_TURNS = 5
// How far past the last reachable point the linear part of the bar runs.
const REACH_MARGIN = 1.08

// The approved mockup's own colours, raw: a dimmed theme colour is drawn in the
// theme's grey, which turned every zone grey (operator, 2026-11-08).
const TRACK = '#33373f' // calm, to attention
const ZONE_ATTENTION = '#6b5a2e' // attention to /second-wind
const ZONE_PAST = '#5e2e33' // /second-wind to auto-compact
const ZONE_NEVER = '#2a2d33' // auto-compact to the end of the window
const SHADOW = '#5d5891' // the next turns at the current pace
const GROWTH = '#c4bfff' // last turn's growth of Messages
const END = '#9aa0aa' // the end-of-window tick
const BASE = '#b0b0b0' // the base label
// Columns kept after the bar for the right text, more when the text is longer.
const RIGHT_RESERVE = 34
const MIN_CELLS = 20

const SKILLS = 'Skills'
const MEMORY = 'Memory files'
const MESSAGES = 'Messages'

// ============================================================
// Formatting
// ============================================================

const stripZero = (s: string): string => s.replace(/\.0$/, '')

// 847, 4.2k, 4k, 312k, 1.2M; 999,600 reads 1M, never 1000k.
export const tokens = (n: number): string => {
  const v = Math.max(0, Math.round(n))
  if (v < 1000) return `${v}`
  if (v < 10_000) {
    const k = stripZero((v / 1000).toFixed(1))
    if (k !== '10') return `${k}k`
  }
  const k = Math.round(v / 1000)
  if (k < 1000) return `${k}k`
  return `${stripZero((v / 1_000_000).toFixed(1))}M`
}

// ============================================================
// Thresholds
// ============================================================

// As `env_int` in scripts/utils/checkpoint_paths.py: empty, not an integer, or
// outside 0-100 gives the default.
const percentOf = (raw: string | undefined, fallback: number): number => {
  if (raw === undefined || raw === '') return fallback
  if (!/^\s*[+-]?\d+(?:_\d+)*\s*$/.test(raw)) return fallback
  const v = Number.parseInt(raw.replace(/_/g, ''), 10)
  return v < 0 || v > 100 ? fallback : v
}

// As `config()` in scripts/utils/checkpoint_autonomy.py: soft >= hard falls back to both defaults.
export const thresholdsOf = (softRaw: string | undefined, hardRaw: string | undefined): Thresholds => {
  const soft = percentOf(softRaw, SOFT_DEFAULT)
  const hard = percentOf(hardRaw, HARD_DEFAULT)
  return soft >= hard ? { soft: SOFT_DEFAULT, hard: HARD_DEFAULT } : { soft, hard }
}

// ============================================================
// Figures
// ============================================================

export const sumOf = (rows: readonly { tokens: number }[]): number => rows.reduce((n, c) => n + c.tokens, 0)

export const messagesOf = (rows: readonly Row[]): number => rows.find(r => r.name === MESSAGES)?.tokens ?? 0

// The turn to append to the history, from the `used` rows above 0.
// `total` is Claude Code's own count when known, so the pace agrees with the
// figure the bar shows; else the estimate.
export const turnOf = (rows: readonly Row[], total?: number | null): Turn => ({
  total: total !== undefined && total !== null && total > 0 ? total : sumOf(rows),
  messages: messagesOf(rows),
})

export const pushTurn = (history: readonly Turn[], t: Turn): Turn[] => [...history, t].slice(-HISTORY_MAX)

// The fixed rows in their own order, then Skills, Memory files and Messages.
export const orderOf = (rows: readonly Row[]): Row[] => {
  const tail = [SKILLS, MEMORY, MESSAGES]
  const fixed = rows.filter(r => !tail.includes(r.name))
  const named = tail.flatMap(n => rows.filter(r => r.name === n))
  return [...fixed, ...named]
}

// The points on the bar, all against FULL_WINDOW: attention, the limit (the
// /second-wind point, or the auto-compaction point when that comes first), the
// auto-compaction point when it comes after the limit, and the end of the window.
export type Limit = { value: number; attention: number; name: string; autoCompact?: number; full: number }

export const limitOf = (t: Thresholds, autoCompact?: number, full: number = FULL_WINDOW): Limit => {
  const hard = (t.hard / 100) * full
  const attention = (t.soft / 100) * full
  const ac = autoCompact !== undefined && autoCompact > 0 ? autoCompact : undefined
  if (ac !== undefined && ac < hard) return { value: ac, attention, name: 'auto-compact', full }
  return { value: hard, attention, name: '/second-wind', autoCompact: ac !== undefined && ac < full ? ac : undefined, full }
}

// Last turn's growth of Messages, and the pace of the used total per turn.
export const forecastOf = (history: readonly Turn[]): { growth: number; pace: number | undefined } => {
  const n = history.length
  if (n < 2) return { growth: 0, pace: undefined }
  const growth = history[n - 1]!.messages - history[n - 2]!.messages
  const pace = (history[n - 1]!.total - history[0]!.total) / (n - 1)
  return { growth, pace: pace > 0 ? pace : undefined }
}

// ============================================================
// Panel header
// ============================================================

export const panelHeaderOf = (used: number, limit: Limit | undefined): { text: string; isOver: boolean } => {
  if (limit === undefined) return { text: `context ${tokens(used)}`, isOver: false }
  const share = `context ${tokens(used)} of ${tokens(limit.full)} (${Math.round((used / limit.full) * 100)}%)`
  if (used > limit.value) {
    return { text: `${share}, past ${limit.name} (${tokens(limit.value)}) by ${tokens(used - limit.value)}`, isOver: true }
  }
  return { text: `${share}, ${limit.name} at ${tokens(limit.value)}`, isOver: false }
}

// ============================================================
// Cells
// ============================================================

type Segment = { name: string; color: string; cells: number }

// Every non-zero row keeps at least one cell; the bar's filled part is the used
// share of max(limit, used), so past the limit the bar fills the width. With
// more rows than cells, the smallest rows share the last cell.
const segmentsOf = (rows: Row[], limit: number, width: number): Segment[] => {
  const w = Math.max(0, Math.floor(width))
  if (w === 0 || rows.length === 0) return []
  const used = sumOf(rows)
  const d = Math.max(limit, used)
  let drawn = rows
  if (rows.length > w) {
    const bySize = [...rows].sort((a, b) => b.tokens - a.tokens)
    const kept = new Set(bySize.slice(0, w - 1))
    const rest = bySize.slice(w - 1)
    const merged: Row = { ...rest[0]!, tokens: sumOf(rest) }
    drawn = [...rows.filter(c => kept.has(c)), merged]
  }
  const exact = drawn.map(c => (c.tokens / d) * w)
  const cells = exact.map(x => Math.max(1, Math.floor(x)))
  // The filled cells end at the used share, raised to one per drawn row and
  // never past the width, in both directions.
  const target = Math.min(w, Math.max(drawn.length, Math.floor((used / d) * w)))
  let total = cells.reduce((n, x) => n + x, 0)
  while (total > target) {
    let at = -1
    for (let i = 0; i < cells.length; i++) if (cells[i]! > 1 && (at < 0 || cells[i]! > cells[at]!)) at = i
    if (at < 0) break
    cells[at]! -= 1
    total -= 1
  }
  while (total < target) {
    let at = 0
    for (let i = 1; i < cells.length; i++) if (exact[i]! - cells[i]! > exact[at]! - cells[at]!) at = i
    cells[at]! += 1
    total += 1
  }
  return drawn.map((c, i) => ({ name: c.name, color: c.color, cells: cells[i]! }))
}

// ============================================================
// Label rows
// ============================================================

type Placed = { col: number; runs: Run[]; len: number }

const lenOf = (runs: readonly Run[]): number => runs.reduce((n, r) => n + [...r.text].length, 0)

// Columns [from, to) taken by a placed item, widened by one each side so
// nothing touches it.
const clashes = (col: number, len: number, taken: readonly Placed[]): Placed | undefined =>
  taken.find(p => col < p.col + p.len + 1 && p.col < col + len + 1)

// On its side's first row a label starts inside its own block, at its first
// cell when nothing is in the way, else as far right as it must. When its start
// would leave the block or its end would pass `right` (the last column the row
// may use), it goes to the next row on that side, from its block's first cell
// (moved left only as far as the row's end needs, never left of `left`), and to
// a further row while that one is taken. Nothing is dropped (operator,
// 2026-11-07: "nothing cut-off").
const placeLabel = (runs: Run[], start: number, end: number, left: number, right: number, rows: Placed[][]): void => {
  const len = lenOf(runs)
  let col = start
  for (let hit = clashes(col, len, rows[0]!); hit !== undefined; hit = clashes(col, len, rows[0]!)) {
    col = hit.col + hit.len + 1
  }
  if (col <= end - 1 && col + len <= right) {
    rows[0]!.push({ col, runs, len })
    return
  }
  placeBelow(runs, start, len, left, right, rows)
}

// Into the first extra row (index 1 on) where it clashes with nothing.
const placeBelow = (runs: Run[], start: number, len: number, left: number, right: number, rows: Placed[][]): void => {
  const col = Math.max(left, Math.min(start, right - len))
  for (let r = 1; ; r++) {
    if (rows[r] === undefined) rows[r] = []
    if (clashes(col, len, rows[r]!) === undefined) {
      rows[r]!.push({ col, runs, len })
      return
    }
  }
}

const lineOf = (placed: readonly Placed[], offset: number): Run[] => {
  const runs: Run[] = []
  let at = 0
  for (const p of [...placed].sort((a, b) => a.col - b.col)) {
    const gap = offset + p.col - at
    if (gap > 0) runs.push({ text: ' '.repeat(gap) })
    runs.push(...p.runs)
    at = offset + p.col + p.len
  }
  return runs
}

// ============================================================
// The band
// ============================================================

// The band's rows, top to bottom, or undefined when there is nothing to draw
// (no window, no used rows): the caller then shows the engine's own band.
export const bandOf = (input: BandInput): Run[][] | undefined => {
  const rows = orderOf(input.rows.filter(r => r.tokens > 0))
  if (!(input.window > 0) || rows.length === 0) return undefined
  const limit = limitOf(input.thresholds, input.autoCompact)
  const used = input.total !== undefined && input.total > 0 ? input.total : sumOf(rows)
  const isOver = used > limit.value
  const inAttention = !isOver && used >= limit.attention
  const columns = Math.max(0, Math.floor(input.columns))
  const prefix = `context ${tokens(used)} `
  const pre = prefix.length
  const { growth, pace } = forecastOf(input.history)

  // The right text, always whole: after the bar when the bar keeps MIN_CELLS
  // beside it, else on its own row under the bar.
  // The end of the window; the share of the whole window first, then the forecast.
  const tick: Run[] = isOver ? [] : [{ text: '▏', color: END }]
  const share = `${Math.round((used / limit.full) * 100)}% of ${tokens(limit.full)}`
  const right: Run[] = []
  if (isOver) {
    right.push(
      { text: ` ◀ ${share}`, color: 'error', bold: true },
      { text: ' · ', color: 'subtle' },
      { text: `past ${limit.name} by ${tokens(used - limit.value)}`, color: 'error', bold: true },
    )
  } else {
    right.push({ text: ` ${share}`, color: inAttention ? 'warning' : 'text', bold: true })
    if (pace !== undefined) {
      const left = Math.floor((limit.value - used) / pace)
      const head = `${left > 50 ? '50+ turns' : `~${left} turns`} left`
      const tail = ` at +${tokens(pace)} a turn`
      right.push({ text: ' · ', color: 'subtle' })
      if (inAttention) right.push({ text: head + tail, color: 'warning', bold: true })
      else right.push({ text: head, color: 'text', bold: true }, { text: tail, color: 'subtle' })
    }
  }
  const reserve = Math.max(RIGHT_RESERVE, lenOf(tick) + lenOf(right))
  const inline = columns - pre - reserve >= MIN_CELLS
  // The bar never takes more than the row leaves it. Too few cells make no
  // honest gauge, so the engine's own band shows instead.
  const cells = inline ? columns - pre - reserve : columns - pre - lenOf(tick)
  if (cells < MIN_CELLS) return undefined
  // The bar is linear to a little past the last point that can happen
  // (auto-compaction, else the limit); the rest of the window, never reached,
  // is one `⋯` cell before the end (operator, 2026-11-08: no long empty stretch).
  const reach = Math.max(limit.autoCompact ?? limit.value, limit.value)
  const span = Math.max(used, reach * REACH_MARGIN)
  const folded = span < limit.full
  const linear = folded ? cells - 1 : cells
  const scale = (folded ? span : Math.max(limit.full, used)) / linear // tokens per cell

  // The cell under a marker: the first cell starting at or past the point.
  const cellAt = (k: number): number => Math.min(linear - 1, Math.max(0, Math.ceil(k / scale - 1e-9)))
  const attnCell = cellAt(limit.attention)
  const endCell = cellAt(limit.value)
  const hasAttention = limit.attention < limit.value && attnCell < endCell
  const acCell = limit.autoCompact === undefined ? undefined : cellAt(limit.autoCompact)
  const hasAutoCompact = acCell !== undefined && acCell > endCell
  const lastCell = cells - 1

  // Blocks.
  const segments = segmentsOf(rows, linear * scale, linear)
  const filled = segments.reduce((n, s) => n + s.cells, 0)
  const groupOf = (name: string): string => (name === SKILLS || name === MEMORY || name === MESSAGES ? name : 'base')
  const extent = new Map<string, [number, number]>()
  let at = 0
  for (const s of segments) {
    const g = groupOf(s.name)
    const e = extent.get(g)
    if (e === undefined) extent.set(g, [at, at + s.cells])
    else e[1] = at + s.cells
    at += s.cells
  }

  const msgRow = rows.find(r => r.name === MESSAGES)
  const msgExt = extent.get(MESSAGES)
  const hasGrowth = msgRow !== undefined && msgExt !== undefined && growth > 0
  const growthCells = hasGrowth ? Math.min(msgExt![1] - msgExt![0], Math.max(1, Math.round(growth / scale))) : 0

  // The bar row.
  const bar: Run[] = [
    isOver
      ? { text: prefix, color: 'error', bold: true }
      : inAttention
        ? { text: prefix, color: 'warning', bold: true }
        : { text: prefix, color: 'text' },
  ]
  for (const s of segments) {
    if (s.name === MESSAGES && growthCells > 0) {
      if (s.cells > growthCells) bar.push({ text: '█'.repeat(s.cells - growthCells), color: s.color })
      bar.push({ text: '█'.repeat(growthCells), color: GROWTH })
    } else {
      bar.push({ text: '█'.repeat(s.cells), color: s.color })
    }
  }
  // The next turns at the current pace, cut at the limit.
  const shadow =
    !isOver && pace !== undefined ? Math.max(0, Math.min(endCell - filled, Math.round((SHADOW_TURNS * pace) / scale))) : 0
  if (shadow > 0) bar.push({ text: '▒'.repeat(shadow), color: SHADOW })
  // The track, zoned: calm to attention, warning to the limit, the limit's red
  // mark, dim red to auto-compaction, and the rest of the window, never reached.
  const zones: [number, Run][] = [
    [hasAttention ? attnCell : endCell, { text: '░', color: TRACK }],
    [endCell, { text: '▒', color: ZONE_ATTENTION }],
    [endCell + 1, isOver ? { text: '▒', color: ZONE_PAST } : { text: '▏', color: 'error', bold: true }],
    [hasAutoCompact ? acCell! : endCell + 1, { text: '▒', color: ZONE_PAST }],
    [linear, { text: '░', color: ZONE_NEVER }],
  ]
  let from = filled + shadow
  for (const [upTo, run] of zones) {
    const n = Math.min(linear, upTo) - from
    if (n > 0) {
      bar.push({ ...run, text: run.text.repeat(n) })
      from += n
    }
  }
  if (folded) bar.push({ text: '⋯', color: END })

  bar.push(...tick)
  // Under the ▏, moved left only as far as the row's end needs.
  let under: Run[] | undefined
  if (inline) bar.push(...right)
  else under = [{ text: ' '.repeat(Math.max(0, Math.min(pre + cells, columns - lenOf(right)))) }, ...right]

  // Markers on the first upper row, placed first: a marker wins over a label.
  const upper: Placed[][] = [[]]
  const lower: Placed[][] = [[]]
  // `▼ name value` from its cell when it fits, else `name value ▼` ending there.
  const fits = (text: string): boolean => pre + endCell + [...text].length <= columns
  const ahead = `▼ ${limit.name} ${tokens(limit.value)}`
  const endText = fits(ahead) ? ahead : `${limit.name} ${tokens(limit.value)} ▼`
  const endLen = [...endText].length
  const endCol = endText.startsWith('▼') ? endCell : Math.max(-pre, endCell - endLen + 1)
  upper[0]!.push({ col: endCol, runs: [{ text: endText, color: 'error' }], len: endLen })
  // The end of the window, ending above the last cell.
  const fullText = `${tokens(limit.full)} ▼`
  const fullLen = [...fullText].length
  if (lastCell > endCell) {
    const fullRuns: Run[] = [{ text: fullText, color: 'text' }]
    const col = lastCell - fullLen + 1
    if (clashes(col, fullLen, upper[0]!) === undefined) upper[0]!.push({ col, runs: fullRuns, len: fullLen })
    else placeBelow(fullRuns, col, fullLen, -pre, columns - pre, upper)
  }
  // Auto-compaction under the bar, beside the labels there: from its cell when
  // it fits, else ending there.
  if (hasAutoCompact) {
    const acValue = tokens(limit.autoCompact!)
    const acAhead = `╰ auto-compact ${acValue}`
    const acText = pre + acCell! + [...acAhead].length <= columns ? acAhead : `auto-compact ${acValue} ╯`
    const acLen = [...acText].length
    const col = acText.startsWith('╰') ? acCell! : acCell! - acLen + 1
    lower[0]!.push({ col, runs: [{ text: acText, color: 'claude' }], len: acLen })
  }
  if (hasAttention) {
    const attnRuns: Run[] = [{ text: `attention ${tokens(limit.attention)} ▼`, color: 'warning' }]
    const attnLen = lenOf(attnRuns)
    const col = attnCell - attnLen + 1
    if (pre + col >= 0 && clashes(col, attnLen, upper[0]!) === undefined) upper[0]!.push({ col, runs: attnRuns, len: attnLen })
    else placeBelow(attnRuns, col, attnLen, -pre, columns - pre, upper)
  }

  // Labels, alternating: base above, Skills below, Memory above, Messages below.
  const fixedTokens = sumOf(rows.filter(r => groupOf(r.name) === 'base'))
  const colorOf = (name: string): string => rows.find(r => r.name === name)?.color ?? 'subtle'
  const tokensOf = (name: string): number => rows.find(r => r.name === name)?.tokens ?? 0
  const labels: [Placed[][], string, Run[]][] = [
    [upper, 'base', [{ text: `╭ base ${tokens(fixedTokens)}`, color: BASE }]],
    [lower, SKILLS, [{ text: `╰ Skills ${tokens(tokensOf(SKILLS))}`, color: colorOf(SKILLS) }]],
    [upper, MEMORY, [{ text: `╭ Memory ${tokens(tokensOf(MEMORY))}`, color: colorOf(MEMORY) }]],
    [
      lower,
      MESSAGES,
      [
        { text: `╰ Messages ${tokens(tokensOf(MESSAGES))}`, color: colorOf(MESSAGES) },
        ...(hasGrowth ? [{ text: ` +${tokens(growth)}`, color: colorOf(MESSAGES), bold: true }] : []),
      ],
    ],
  ]
  // Labels go left to right, so each one only has to clear what is already placed.
  for (const [side, group, runs] of labels) {
    const e = extent.get(group)
    if (e !== undefined) placeLabel(runs, e[0], e[1], -pre, columns - pre, side)
  }

  return fitted(
    [
      // The first upper row sits on the bar, a row that had to move goes above it.
      ...[...upper].reverse().map(r => lineOf(r, pre)),
      bar,
      ...(under === undefined ? [] : [under]),
      ...lower.map(r => lineOf(r, pre)),
    ],
    columns,
  )
}

// The last word on width: no row may be longer than the columns it is drawn in.
// Nothing above makes one, so a row that is means a layout defect; the band
// then yields to the engine's own rather than show a cut row.
const fitted = (lines: Run[][], columns: number): Run[][] | undefined =>
  lines.every(l => lenOf(l) <= columns) ? lines : undefined
