// context-bar: the context bar, showing what fills the context window, by
// category, measured against the whole 1M window with its attention,
// /second-wind and auto-compact points marked, with last turn's growth and a
// forecast of turns left; and a panel listing every item. The bar is drawn by
// the status line, under the prompt (HOS-369): the mod writes the band's rows
// to one file per session, `context-bar-<session id>.json` under the registry
// name `runtime:hook-state/cache`, which `.claude/hooks/checkpoint-statusline.py`
// prints above its own rows. The mod starts no process: the status line, which
// runs from the engine checkout, tells it where to write through a pointer per
// session, `<HOME>/.claude/context-bar/<session id>.json`, in Claude Code's own
// config folder. With no valid pointer (a session whose status line is not the
// engine's) or a failed write, the band is drawn above the prompt as before.
// Reads `$.session.usage({ breakdown: 'summary' })`, which estimates locally
// and sends no request, and the two handoff thresholds from the environment:
// no network, no model calls. The layout itself is in ./layout.
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, SessionContextBreakdown } from 'claude-code'
import { bandOf, limitOf, panelHeaderOf, pushTurn, sumOf, thresholdsOf, tokens, turnOf } from './layout'
import type { Limit, Row, Run, Thresholds } from './layout'

// The command and the hook matchers name themselves in literals so
// `claude plugin validate` can read what the module answers.
const PANE = 'context-bar'

const isBandOn = atom({ plugin: 'context-bar', key: 'isBandOn' } as const, true)
const figures = atom({ plugin: 'context-bar', key: 'figures' } as const, null)
const contextWindow = atom({ plugin: 'context-bar', key: 'window' } as const, null)
const contextTokens = atom({ plugin: 'context-bar', key: 'tokens' } as const, null)
const history = atom({ plugin: 'context-bar', key: 'history' } as const, [])

// ============================================================
// The status line's file
// ============================================================

// The directory the status line's pointer names for this session, or
// undefined while there is no valid pointer: then the band is drawn above the
// prompt. Never built here (no physical data path, M051).
let barDir: string | undefined
// The status line's own row width, from the same pointer (HOS-420): the rows it
// prints are laid out for it, never for the chat column beside a docked pane.
let barColumns: number | undefined
// The session id and rows last written, so an unchanged render writes nothing.
let lastWritten: string | undefined

// The id becomes part of a file name; the status line applies the same pattern.
const SESSION_ID = /^[A-Za-z0-9-]+$/

// The session the pointer was read for, and when, so a render reads the
// pointer only when the id changes, or while there is none, at most every 5 s.
let pointerFor: string | undefined
let pointerReadAt = 0
const POINTER_EVERY_MS = 5000

// Reads `<HOME>/.claude/context-bar/<id>.json`, which the engine's status line
// writes (HOS-369, F2): the only trusted source of the directory. Any folder a
// session opens is untrusted, so nothing found through it is ever run.
const refreshPointer = async ($: EngineInterface): Promise<void> => {
  const id = await $.session.id()
  const now = Date.now()
  if (id === pointerFor && now - pointerReadAt < POINTER_EVERY_MS) return
  pointerFor = id
  pointerReadAt = now
  barDir = undefined
  barColumns = undefined
  try {
    const home = ((await $.env.get('HOME')) ?? '').replace(/\/+$/, '')
    if (!home.startsWith('/') || !SESSION_ID.test(id)) return
    const p = JSON.parse(await $.fs.read(`${home}/.claude/context-bar/${id}.json`))
    const dir = p?.dir
    if (p?.session !== id || typeof dir !== 'string' || !dir.startsWith('/') || dir.split('/').includes('..')) {
      $.ui.log('context-bar: pointer invalid, bar stays above the prompt', { to: 'debug' })
      return
    }
    barDir = dir.replace(/\/+$/, '')
    const cols = p?.columns
    if (Number.isInteger(cols) && cols >= 20 && cols <= 1000) barColumns = cols
  } catch (err) {
    // No pointer: the engine's status line does not draw in this session.
    $.ui.log(`context-bar: no pointer: ${String(err)}`, { to: 'debug' })
  }
}

// Writes this session's rows for the status line; true when the file holds
// them (written now or before), false when the write failed.
const writeRows = async ($: EngineInterface, rows: Run[][], columns: number): Promise<boolean> => {
  try {
    const id = await $.session.id()
    if (!SESSION_ID.test(id)) throw new Error(`session id not usable in a file name: ${JSON.stringify(id)}`)
    const key = id + '\n' + JSON.stringify(rows)
    if (key === lastWritten) return true
    const body = { session: id, written_at: new Date().toISOString(), columns, rows }
    await $.fs.write(`${barDir}/context-bar-${id}.json`, JSON.stringify(body))
    lastWritten = key
    return true
  } catch (err) {
    lastWritten = undefined
    $.ui.log(`context-bar: write for the status line failed: ${String(err)}`, { to: 'debug' })
    return false
  }
}

// ============================================================
// Figures
// ============================================================

// The rows that occupy the window, never `free`, `buffer` or `deferred`.
const usedOf = (f: SessionContextBreakdown): Row[] =>
  f.categories.filter(c => c.kind === 'used' && c.tokens > 0).map(c => ({ name: c.name, tokens: c.tokens, color: c.color }))

// The handoff menu's two points, read as scripts/utils/checkpoint_autonomy.py reads them.
const thresholdsNow = async ($: EngineInterface): Promise<Thresholds> =>
  thresholdsOf(await $.env.get('CLAUDE_HANDOFF_SOFT_THRESHOLD'), await $.env.get('CLAUDE_HANDOFF_HARD_THRESHOLD'))

// The auto-compact point: the configured auto-compact window
// (`CLAUDE_CODE_AUTO_COMPACT_WINDOW`, the environment twin of the
// `autoCompactWindow` setting), marked even while auto-compaction is off
// (operator decision 2026-11-08); without it, the engine's own threshold when on.
const autoCompactNow = async ($: EngineInterface, f: SessionContextBreakdown): Promise<number | undefined> => {
  const raw = (await $.env.get('CLAUDE_CODE_AUTO_COMPACT_WINDOW'))?.trim() ?? ''
  if (/^\d+$/.test(raw) && Number(raw) > 0) return Number(raw)
  return f.isAutoCompactEnabled ? f.autoCompactThreshold : undefined
}

const limitNow = async ($: EngineInterface, f: SessionContextBreakdown, w: number | null): Promise<Limit | undefined> =>
  w !== null && w > 0 ? limitOf(await thresholdsNow($), await autoCompactNow($, f)) : undefined

// ============================================================
// State changes
// ============================================================

const isPanelOpen = async ($: EngineInterface): Promise<boolean> => (await $.ui.panes()).some(p => p.id === PANE)

// Stores the figures and the window; returns the figures, or null when the estimate failed.
const refresh = async ($: EngineInterface): Promise<SessionContextBreakdown | null> => {
  let got: SessionContextBreakdown | null = null
  let size: number | null = null
  let count: number | null = null
  try {
    const context = (await $.session.usage({ breakdown: 'summary' })).context
    got = context.breakdown ?? null
    size = context.window > 0 ? context.window : null
    count = context.tokens !== undefined && context.tokens > 0 ? context.tokens : null
  } catch (err) {
    // A failed estimate draws nothing; the debug log says why.
    $.ui.log(`usage failed: ${String(err)}`, { to: 'debug' })
    got = null
    size = null
    count = null
  }
  await update($, figures, () => got)
  await update($, contextWindow, () => size)
  await update($, contextTokens, () => count)
  return got
}

const refreshIfShown = async ($: EngineInterface): Promise<void> => {
  if ((await read($, isBandOn)) || (await isPanelOpen($))) await refresh($)
}

const forget = async ($: EngineInterface): Promise<void> => {
  await update($, history, () => [])
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
  // Every main turn adds to the history, the band shown or not, so the forecast
  // is ready when it is turned on; a failed estimate adds nothing.
  on('turn.complete', async ($, e, next) => {
    const r = await next(e)
    if (!e.agentId) {
      const got = await refresh($)
      if (got !== null) {
        const turn = turnOf(usedOf(got), await read($, contextTokens))
        await update($, history, h => pushTurn(h, turn))
      }
    }
    return r
  }).catch(($, e, next) => {
    $.ui.log(`turn.complete hook failed: ${String(next.error)}`, { to: 'debug' })
    return next(e)
  })

  // A main compaction that compacted: not a precompute, not a skip. The pace
  // before it says nothing about the pace after it.
  on('session.compact', async ($, e, next) => {
    const r = await next(e)
    if (!e.agentId && e.trigger !== 'precompute' && r.skip === undefined) {
      await forget($)
      await refreshIfShown($)
    }
    return r
  }).catch(($, e, next) => {
    $.ui.log(`session.compact hook failed: ${String(next.error)}`, { to: 'debug' })
    return next(e)
  })

  // `/clear` ends the conversation with no `session.start` after it.
  on('session.end', async ($, e, next) => {
    await forget($)
    if (e.reason === 'clear') {
      await update($, figures, () => null)
      await update($, contextWindow, () => null)
      await update($, contextTokens, () => null)
    }
    return next(e)
  })

  // ============================================================
  // Drawing
  // ============================================================

  // The band yields to a survey and to any inner hook that draws (agent-watch's
  // batch row among them), and draws only over the engine's own empty band.
  // While the status line's file is written, the status line draws the bar
  // and nothing is drawn here; with nothing to draw, the file holds no rows.
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey) return next(e)
    await refreshPointer($)
    const isOn = await read($, isBandOn)
    const f = await read($, figures)
    // Rows for the status line are laid out at its own width when the pointer
    // gave one (HOS-420); the band above the prompt keeps the chat column.
    const toBar = barDir !== undefined && barColumns !== undefined
    const width = toBar && barColumns !== undefined ? barColumns : e.props.bodyColumns
    if (!isOn || f === null) {
      if (barDir !== undefined) await writeRows($, [], width)
      return next(e)
    }
    const inner = await next(e)
    if (inner.type !== 'engine') return inner
    const input = {
      rows: usedOf(f),
      window: (await read($, contextWindow)) ?? 0,
      total: (await read($, contextTokens)) ?? undefined,
      thresholds: await thresholdsNow($),
      autoCompact: await autoCompactNow($, f),
      history: await read($, history),
    }
    let lines = bandOf({ ...input, columns: width })
    if (lines === undefined) {
      if (barDir !== undefined) await writeRows($, [], width)
      return inner
    }
    // No theme API turns a key into RGB (2.1.312 types), so the colours stay
    // theme keys and the status line maps them.
    if (barDir !== undefined && (await writeRows($, lines, width))) return inner
    if (toBar) {
      lines = bandOf({ ...input, columns: e.props.bodyColumns })
      if (lines === undefined) return inner
    }
    const { Box, Text } = $.ui.resolve(e)
    const run = (r: Run, i: number) => (
      <Text key={`r${i}`} color={r.color} bold={r.bold} dimColor={r.dim}>
        {r.text}
      </Text>
    )
    return (
      <Box flexDirection="column">
        {lines.map((line, n) => (
          <Text key={`l${n}`} wrap="truncate-end">
            {line.length > 0 ? line.map(run) : ' '}
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
    const header = panelHeaderOf((await read($, contextTokens)) ?? sumOf(rows), await limitNow($, f, await read($, contextWindow)))
    const desc = <T extends { tokens: number }>(list: readonly T[]): T[] => [...list].sort((a, b) => b.tokens - a.tokens)
    const groups: [string, { name: string; tokens: number }[]][] = [
      ['Memory files', desc(f.memoryFiles).map(m => ({ name: m.path, tokens: m.tokens }))],
      ['MCP tools', desc(f.mcpTools).map(t => ({ name: t.name, tokens: t.tokens }))],
      ['Skills', desc(f.skills?.skillFrontmatter ?? []).map(s => ({ name: s.name, tokens: s.tokens }))],
      ['Agents', desc(f.agents).map(a => ({ name: a.agentType, tokens: a.tokens }))],
    ]
    return (
      <Box flexDirection="column">
        {header.isOver ? (
          <Text color="error" wrap="truncate-end">
            {header.text}
          </Text>
        ) : (
          <Text wrap="truncate-end">{header.text}</Text>
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
