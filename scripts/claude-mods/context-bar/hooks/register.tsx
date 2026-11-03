// context-bar: a band above the prompt showing what fills the context window,
// by category, against the auto-compaction point, and a panel listing every
// item. Reads only `$.session.usage({ breakdown: 'summary' })`, which estimates
// locally and sends no request: no network, no model calls, no files.
import { atom, read, update } from 'claude-code'
import type { ContextCategory, EngineInterface, Register, SessionContextBreakdown } from 'claude-code'

// The command and the hook matchers name themselves in literals so
// `claude plugin validate` can read what the module answers.
const PANE = 'context-bar'

const isBandOn = atom({ plugin: 'context-bar', key: 'isBandOn' } as const, true)
const figures = atom({ plugin: 'context-bar', key: 'figures' } as const, null)

// ============================================================
// Formatting
// ============================================================

const stripZero = (s: string): string => s.replace(/\.0$/, '')

// 847, 4.2k, 4k, 312k, 1.2M; 999,600 reads 1M, never 1000k.
const tokens = (n: number): string => {
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

// The rows that occupy the window, never `free`, `buffer` or `deferred`.
const usedOf = (f: SessionContextBreakdown): ContextCategory[] =>
  f.categories.filter(c => c.kind === 'used' && c.tokens > 0)

const sumOf = (rows: ContextCategory[]): number => rows.reduce((n, c) => n + c.tokens, 0)

// What the band draws against: the auto-compaction point while auto-compaction
// is on, else the context window itself, so the band still shows the fill when
// a workspace turns auto-compaction off and compacts by its own rules.
type Limit = { value: number; toCompaction: boolean }

const headerOf = (used: number, limit: Limit | undefined): string => {
  if (limit === undefined) return `context ${tokens(used)}`
  const pct = Math.round((used / limit.value) * 100)
  const point = limit.toCompaction ? 'to compaction' : 'window, auto-compaction off'
  const head = `context ${tokens(used)} of ${tokens(limit.value)} ${point} (${pct}%)`
  if (used <= limit.value) return head
  const over = limit.toCompaction ? 'over the compaction point' : 'over the window'
  return `${head}, ${over} by ${tokens(used - limit.value)}`
}

const limitOf = (f: SessionContextBreakdown): Limit | undefined => {
  if (f.isAutoCompactEnabled && f.autoCompactThreshold !== undefined && f.autoCompactThreshold > 0) {
    return { value: f.autoCompactThreshold, toCompaction: true }
  }
  return f.maxTokens > 0 ? { value: f.maxTokens, toCompaction: false } : undefined
}

// ============================================================
// Cells
// ============================================================

type Segment = { color: string; cells: number }

// Every non-zero row keeps at least one cell; the bar's filled part is the used
// share of max(threshold, used), so past the threshold the bar fills the width.
// With more rows than cells, the smallest rows share the last cell.
const segmentsOf = (rows: ContextCategory[], threshold: number, width: number): { segments: Segment[]; empty: number } => {
  const w = Math.max(0, Math.floor(width))
  if (w === 0 || rows.length === 0) return { segments: [], empty: w }
  const used = sumOf(rows)
  const d = Math.max(threshold, used)
  let drawn = rows
  if (rows.length > w) {
    const bySize = [...rows].sort((a, b) => b.tokens - a.tokens)
    const kept = new Set(bySize.slice(0, w - 1))
    const rest = bySize.slice(w - 1)
    const merged: ContextCategory = { ...rest[0]!, tokens: sumOf(rest) }
    drawn = [...rows.filter(c => kept.has(c)), merged]
  }
  const exact = drawn.map(c => (c.tokens / d) * w)
  const cells = exact.map(x => Math.max(1, Math.floor(x)))
  // The filled cells end at the used share, raised to one per drawn row and
  // never past the width, in both directions: the one-cell minimums can
  // overshoot the share as the floors can undershoot it.
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
  return { segments: drawn.map((c, i) => ({ color: c.color, cells: cells[i]! })), empty: Math.max(0, w - total) }
}

// ============================================================
// Legend
// ============================================================

const SEP = '  '

// At most two rows of `■ <name> <tokens>`; when they cannot hold every row, the
// last entry that fits is followed by `+<n> more in /context-bar panel`.
const legendOf = (rows: ContextCategory[], width: number): { lines: ContextCategory[][]; more: number } => {
  const w = Math.max(1, Math.floor(width))
  const entry = (c: ContextCategory): number => `■ ${c.name} ${tokens(c.tokens)}`.length
  const lines: ContextCategory[][] = [[], []]
  const lens = [0, 0]
  let line = 0
  let i = 0
  for (; i < rows.length; i++) {
    const c = rows[i]!
    const add = (lines[line]!.length > 0 ? SEP.length : 0) + entry(c)
    if (lens[line]! + add <= w || lines[line]!.length === 0) {
      lines[line]!.push(c)
      lens[line]! += add
      continue
    }
    if (line === 1) break
    line = 1
    i--
  }
  let more = rows.length - i
  if (more > 0) {
    const tail = (n: number): number => (lines[1]!.length > 0 ? SEP.length : 0) + `+${n} more in /context-bar panel`.length
    while (lines[1]!.length > 0 && lens[1]! + tail(more) > w) {
      const c = lines[1]!.pop()!
      lens[1]! -= (lines[1]!.length > 0 ? SEP.length : 0) + entry(c)
      more += 1
    }
  }
  // The second row stays, entries or none, when it carries the `+<n> more`.
  return { lines: more > 0 ? lines : lines.filter(l => l.length > 0), more }
}

// ============================================================
// State changes
// ============================================================

const isPanelOpen = async ($: EngineInterface): Promise<boolean> => (await $.ui.panes()).some(p => p.id === PANE)

const refresh = async ($: EngineInterface): Promise<void> => {
  let got: SessionContextBreakdown | null = null
  try {
    got = (await $.session.usage({ breakdown: 'summary' })).context.breakdown ?? null
  } catch (err) {
    // A failed estimate draws nothing; the debug log says why.
    $.ui.log(`usage failed: ${String(err)}`, { to: 'debug' })
    got = null
  }
  await update($, figures, () => got)
}

const refreshIfShown = async ($: EngineInterface): Promise<void> => {
  if ((await read($, isBandOn)) || (await isPanelOpen($))) await refresh($)
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    await $.command.register({
      name: 'context-bar',
      description: 'Show or hide the context bar; /context-bar panel opens the full list.',
    })
    // On by default: fetch the figures now, so the band shows before the first turn ends.
    await refreshIfShown($)
    return started
  })

  on('command.run', { command: 'context-bar' }, async ($, e) => {
    const arg = e.args.trim()
    if (arg === '') {
      const isOn = !(await read($, isBandOn))
      await update($, isBandOn, () => isOn)
      if (isOn) await refresh($)
      return { text: isOn ? 'Context bar on.' : 'Context bar off.' }
    }
    if (arg === 'panel') {
      if (await isPanelOpen($)) {
        await $.ui.close({ id: PANE })
        return { text: 'Context panel closed.' }
      }
      await refresh($)
      await $.ui.open({ id: PANE, title: 'Context' })
      return { text: 'Context panel opened.' }
    }
    return { text: 'Usage: /context-bar, or /context-bar panel.' }
  })

  // The main conversation's turn only: a subagent's end leaves the figures alone.
  on('turn.complete', async ($, e, next) => {
    const r = await next(e)
    if (!e.agentId) await refreshIfShown($)
    return r
  }).catch(($, e, next) => {
    $.ui.log(`turn.complete hook failed: ${String(next.error)}`, { to: 'debug' })
    return next(e)
  })

  // A main compaction that compacted: not a precompute, not a skip.
  on('session.compact', async ($, e, next) => {
    const r = await next(e)
    if (!e.agentId && e.trigger !== 'precompute' && r.skip === undefined) await refreshIfShown($)
    return r
  }).catch(($, e, next) => {
    $.ui.log(`session.compact hook failed: ${String(next.error)}`, { to: 'debug' })
    return next(e)
  })

  // `/clear` ends the conversation with no `session.start` after it.
  on('session.end', async ($, e, next) => {
    if (e.reason === 'clear') await update($, figures, () => null)
    return next(e)
  })

  // ============================================================
  // Drawing
  // ============================================================

  // The band yields to a survey and to any inner hook that draws (agent-watch's
  // batch row among them), and draws only over the engine's own empty band.
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey) return next(e)
    const isOn = await read($, isBandOn)
    const f = await read($, figures)
    if (!isOn || f === null) return next(e)
    const inner = await next(e)
    if (inner.type !== 'engine') return inner
    const limit = limitOf(f)
    const rows = usedOf(f)
    if (limit === undefined || rows.length === 0) return inner
    const { Box, Text } = $.ui.resolve(e)
    const width = e.props.bodyColumns
    const used = sumOf(rows)
    const isOver = used > limit.value
    const { segments, empty } = segmentsOf(rows, limit.value, width)
    const legend = legendOf(rows, width)
    return (
      <Box flexDirection="column">
        {isOver ? (
          <Text color="error" wrap="truncate-end">
            {headerOf(used, limit)}
          </Text>
        ) : (
          <Text wrap="truncate-end">{headerOf(used, limit)}</Text>
        )}
        <Text wrap="truncate-end">
          {segments.map((s, i) => (
            <Text key={`c${i}`} color={s.color}>
              {'█'.repeat(s.cells)}
            </Text>
          ))}
          {empty > 0 && <Text dimColor>{'░'.repeat(empty)}</Text>}
        </Text>
        {legend.lines.map((line, n) => (
          <Text key={`l${n}`} wrap="truncate-end">
            {line.map((c, i) => (
              <Text key={`e${i}`} color={c.color}>
                {i > 0 ? SEP : ''}■ {c.name} {tokens(c.tokens)}
              </Text>
            ))}
            {n === legend.lines.length - 1 && legend.more > 0 && (
              <Text dimColor>
                {line.length > 0 ? SEP : ''}+{legend.more} more in /context-bar panel
              </Text>
            )}
          </Text>
        ))}
      </Box>
    )
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const f = await read($, figures)
    if (f === null) {
      return (
        <Box flexDirection="column">
          <Text dimColor wrap="wrap">
            No context figures yet: they arrive when the next turn ends.
          </Text>
        </Box>
      )
    }
    const rows = usedOf(f).sort((a, b) => b.tokens - a.tokens)
    const limit = limitOf(f)
    const used = sumOf(rows)
    const isOver = limit !== undefined && used > limit.value
    const desc = <T extends { tokens: number }>(list: readonly T[]): T[] => [...list].sort((a, b) => b.tokens - a.tokens)
    const groups: [string, { name: string; tokens: number }[]][] = [
      ['Memory files', desc(f.memoryFiles).map(m => ({ name: m.path, tokens: m.tokens }))],
      ['MCP tools', desc(f.mcpTools).map(t => ({ name: t.name, tokens: t.tokens }))],
      ['Skills', desc(f.skills?.skillFrontmatter ?? []).map(s => ({ name: s.name, tokens: s.tokens }))],
      ['Agents', desc(f.agents).map(a => ({ name: a.agentType, tokens: a.tokens }))],
    ]
    return (
      <Box flexDirection="column">
        {isOver ? (
          <Text color="error" wrap="truncate-end">
            {headerOf(used, limit)}
          </Text>
        ) : (
          <Text wrap="truncate-end">{headerOf(used, limit)}</Text>
        )}
        {rows.map((c, i) => (
          <Text key={`r${i}`} color={c.color} wrap="truncate-end">
            ■ {c.name} {tokens(c.tokens)}
          </Text>
        ))}
        {groups
          .filter(([, items]) => items.length > 0)
          .map(([label, items]) => (
            <Box key={label} flexDirection="column" marginTop={1}>
              <Text bold>{label}</Text>
              {items.map((it, i) => (
                <Text key={`${label}${i}`} wrap="truncate-end">
                  {it.name} {tokens(it.tokens)}
                </Text>
              ))}
            </Box>
          ))}
      </Box>
    )
  })
}
