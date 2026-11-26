// lot-map: a pane beside the chat mapping where Claude's work goes. Every tool
// call, model step, hook and message lights one of ten stations; the pane shows
// the board (calls, errors, time, context growth per station), the route the
// main loop took, what runs now, and the last rows.
//
// A rebuild of the lot-map mod (github.com/alexeyw/lot-map) from its README
// alone, read 2026-11-22: no line of its code was read or copied. Its station
// and label rules are shared, by port, with scripts/lot-map.py, the tool for
// past sessions.
//
// `/lot-map` (or `/lot-map on`) opens the pane, `/lot-map off` closes it,
// `/lot-map toggle` flips it. Nothing opens by itself at the start of a session;
// a reload reopens a pane the person had on.
//
// Reads only what the engine hands its hooks and one environment variable,
// HC_TERMINAL_ID, which the new Herdr (herdr-core) sets: under it the map is a
// picture (Kitty graphics), elsewhere braille. While a call runs and the pane
// shows the picture, and while the model works on a turn, a timer swaps the
// picture for animation frames (HOS-425); it stops when nothing runs, when the
// pane closes, on `/lot-map off` and after ten minutes with no event, so an idle
// session draws nothing between events. No network, no model calls, no
// files, no processes. Every hook passes the event on unchanged and returns what
// came back unchanged; a fault inside the mod is counted and logged to the debug
// log, and never reaches a tool call or a turn.
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Board, Meta, Row, RowKind, RowState } from '../types'
import { NAMES, STATIONS, clean, isMemoryText, shortModel, toolStation } from './stations'
import { renderBraille, roundHalfEven } from './braille'
import { drawFrame, encodePng, renderPicture, sceneFor, toBase64 } from './picture'
import type { Scene } from './picture'
import type { Tone } from './braille'
import type { Station } from './stations'

const PANE = 'lot-map'
const TITLE = 'Lot map'
const MAX_ROWS = 200
const ROUTE_LEN = 6
const WINDOW = 1_000_000 // context-bar's FULL_WINDOW, operator decision 2026-11-08

const emptyBoard = (): Board =>
  Object.fromEntries(STATIONS.map(s => [s, { calls: 0, errors: 0, ms: 0, ctx: 0, hookMemory: 0 }])) as Board

const emptyMeta = (): Meta => ({
  model: '', context: 0, steps: 0, turns: 0, turnStart: 0, pending: [], compacted: false,
})

// Change a tag when that value's fields change: it declines what an earlier
// version stored, which outlives a reload.
const rows = atom({ plugin: 'lot-map', key: 'rows' } as const, [], { shape: 'rows-1' })
const board = atom({ plugin: 'lot-map', key: 'board' } as const, emptyBoard(), { shape: 'board-1' })
const route = atom({ plugin: 'lot-map', key: 'route' } as const, [], { shape: 'route-1' })
const meta = atom({ plugin: 'lot-map', key: 'meta' } as const, emptyMeta(), { shape: 'meta-1' })
const isOn = atom({ plugin: 'lot-map', key: 'isOn' } as const, false)
const faults = atom({ plugin: 'lot-map', key: 'faults' } as const, 0)

// ============================================================
// Recording
// ============================================================

let seq = 0
const newId = (): string => `r${Date.now().toString(36)}${(seq++).toString(36)}`

// The newest MAX_ROWS rows, but never a call still running: dropping its row would
// make the pane say nothing runs and give the finished call no duration. Calls that
// never report their end can still pile up, so past HARD_ROWS the oldest go anyway.
const HARD_ROWS = 2 * MAX_ROWS
const trim = (list: Row[]): Row[] => {
  if (list.length <= MAX_ROWS) return list
  let drop = list.length - MAX_ROWS
  const kept = list.filter(r => {
    if (drop > 0 && r.state !== 'running') { drop -= 1; return false }
    return true
  })
  return kept.length > HARD_ROWS ? kept.slice(-HARD_ROWS) : kept
}

// When work was last recorded, in this process's wall time: every event the map
// shows passes through addRow or tally. The motion's ten-minute cutoff reads it.
let lastWork = 0

const addRow = async ($: EngineInterface, row: Row): Promise<void> => {
  lastWork = Date.now()
  await update($, rows, list => trim([...list, { ...row, label: clean(row.label, 200) }]))
}

const tally = async ($: EngineInterface, station: Station, change: Partial<Board[Station]>): Promise<void> => {
  lastWork = Date.now()
  await update($, board, b => {
    const t = b[station]
    return {
      ...b,
      [station]: {
        calls: t.calls + (change.calls ?? 0),
        errors: t.errors + (change.errors ?? 0),
        ms: t.ms + (change.ms ?? 0),
        ctx: t.ctx + (change.ctx ?? 0),
        hookMemory: t.hookMemory + (change.hookMemory ?? 0),
      },
    }
  })
}

const event = async ($: EngineInterface, station: Station, label: string, kind: RowKind,
  state: RowState = 'done', start?: number): Promise<void> => {
  const at = await $.clock.now()
  await addRow($, { id: newId(), station, label, state, kind, sub: false, start: start ?? at, end: at })
  if (kind !== 'answer' && kind !== 'switch') {
    await tally($, station, { calls: 1, errors: state === 'failed' || state === 'refused' ? 1 : 0 })
  }
}

// A fault inside the mod: counted for the pane's footer and named in the debug log.
const fault = async ($: EngineInterface, where: string, error: unknown): Promise<void> => {
  $.ui.log(`lot-map: ${where} failed: ${error instanceof Error ? error.name : 'error'}`, { to: 'debug' })
  await update($, faults, n => n + 1)
}

const promptOf = (u: { input_tokens?: number; cache_read_input_tokens?: number; cache_creation_input_tokens?: number }): number =>
  (u.input_tokens || 0) + (u.cache_read_input_tokens || 0) + (u.cache_creation_input_tokens || 0)

// The README's context cost: a step's growth goes to the calls whose answers
// went with it; one call takes it whole, two or three share it evenly (~), more
// get none on their rows, and the station's sum keeps it when all share one station.
const spread = async ($: EngineInterface, ids: string[], growth: number): Promise<void> => {
  if (ids.length === 0 || growth <= 0) return
  const list = await read($, rows)
  const calls = list.filter(r => ids.includes(r.id))
  if (calls.length === 0) return
  const stations = new Set(calls.map(r => r.station))
  if (calls.length > 3 && stations.size > 1) return
  const each = calls.length <= 3 ? Math.floor(growth / calls.length) : 0
  const shared = calls.length > 1
  await update($, rows, all =>
    all.map(r => (ids.includes(r.id) && calls.length <= 3 ? { ...r, ctx: each, shared } : r)),
  )
  if (calls.length <= 3) for (const r of calls) await tally($, r.station, { ctx: each })
  else if (calls[0]) await tally($, calls[0].station, { ctx: growth })
}

const textOf = (content: readonly unknown[]): string =>
  content
    .map(b => (b && typeof b === 'object' && (b as { type?: unknown }).type === 'text' ? String((b as { text?: unknown }).text ?? '') : ''))
    .join(' ')
    .slice(0, 4000)

// ============================================================
// The pane
// ============================================================

// ============================================================
// Motion
// ============================================================

// 12 frames a second; 8 when a frame costs more than SLOW_AT ms to make (measured
// 2026-11-23 on the operator's machine, load under 1 on 16 cores: about 18 ms at 48
// columns, 35 at 76).
const FRAME_MS = 80
const SLOW_MS = 125
const SLOW_AT = 20
// A turn whose end never arrives must not animate for ever: with no work event
// for this long by the wall clock, the motion stops until the next one.
const STALE_MS = 10 * 60_000

type Motion = { timer: { cancel: () => void }; ms: number; frames: number; cost: number; isBusy: boolean }

// The running animation, the scene its frames draw, and whether the engine refused
// a frame: then the still picture stays until the pane is switched on again.
let motion: Motion | undefined
let staged: { scene: Scene; cols: number; rows: number } | undefined
let seconds = 0
let isMotionRefused = false

const stopMotion = (): void => {
  motion?.timer.cancel()
  motion = undefined
}

const startMotion = ($: EngineInterface, ms: number): void => {
  stopMotion()
  const m: Motion = { timer: { cancel: () => undefined }, ms, frames: 0, cost: 0, isBusy: false }
  motion = m
  m.timer = $.clock.every(ms, () => { void tick($, m) })
}

// One frame: drawn over the scene, encoded, and swapped into the keyed Image with
// no redraw. A frame still on its way when the next is due is skipped.
const tick = async ($: EngineInterface, m: Motion): Promise<void> => {
  if (motion !== m || !staged || m.isBusy) return
  if (Date.now() - lastWork > STALE_MS) {
    stopMotion()
    staged = undefined
    return
  }
  m.isBusy = true
  try {
    const { scene, cols, rows } = staged
    const started = Date.now()
    seconds += m.ms / 1000
    const source = { png: toBase64(encodePng(drawFrame(scene, seconds), scene.width, scene.height)) }
    const cost = Date.now() - started
    m.frames += 1
    m.cost = m.frames === 1 ? cost : 0.8 * m.cost + 0.2 * cost
    const done = await $.ui.blit({ requestId: PANE, key: 'map', source, columns: cols, rows })
    // A redraw that took the picture off the pane stopped this motion while the
    // frame was on its way; the engine refusing that frame is expected, not a fault.
    if (motion !== m) return
    if (done.deny !== undefined) {
      stopMotion()
      isMotionRefused = true
      $.ui.log(`lot-map: a motion frame was refused${done.fix ? ` (${done.fix})` : ''}; the still picture stays`, { to: 'debug' })
      await update($, faults, n => n + 1)
      $.ui.invalidate('ui.render')
      return
    }
    if (motion === m && m.ms === FRAME_MS && m.frames >= 6 && m.cost > SLOW_AT) {
      $.ui.log(`lot-map: a frame costs about ${Math.round(m.cost)} ms; motion slowed to ${Math.round(1000 / SLOW_MS)} frames a second`, { to: 'debug' })
      startMotion($, SLOW_MS)
    }
  } catch (error) {
    stopMotion()
    isMotionRefused = true
    await fault($, 'motion', error)
  } finally {
    m.isBusy = false
  }
}

const isOpenNow = async ($: EngineInterface): Promise<boolean> => (await $.ui.panes()).some(p => p.id === PANE)

const setPane = async ($: EngineInterface, on: boolean): Promise<void> => {
  await update($, isOn, () => on)
  if (on) isMotionRefused = false
  else {
    stopMotion()
    staged = undefined
  }
  if (on) await $.ui.open({ id: PANE, title: TITLE, backgroundColor: 'transparent' })
  else if (await isOpenNow($)) await $.ui.close({ id: PANE })
}

const k = (n: number | undefined, shared = false): string => {
  if (!n) return ''
  return `${shared ? '~' : ''}+${n >= 1000 ? `${roundHalfEven(n / 1000)}k` : String(n)}`
}

const dur = (ms: number | undefined): string => {
  if (ms === undefined) return '-'
  if (ms < 1000) return `${Math.round(ms)} ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`
  const m = Math.floor(ms / 60_000)
  const s = Math.floor((ms % 60_000) / 1000)
  return m < 60 ? `${m}m${String(s).padStart(2, '0')}s` : `${Math.floor(m / 60)}h${String(m % 60).padStart(2, '0')}m`
}

const MARK: Record<RowState, string> = { running: '●', done: '·', failed: '✕', refused: '✕', info: '◆' }

// Consecutive done calls at one station fold into one row, `+N` for the rest.
// Running rows are never pushed out by what came after them: they keep half the list at most.
export const fold = (list: Row[], limit: number): [Row, number][] => {
  const out: [Row, number][] = []
  for (const r of list) {
    const prev = out[out.length - 1]
    if (prev && r.kind === 'call' && prev[0].kind === 'call' && r.state === 'done' && prev[0].state === 'done'
      && prev[0].station === r.station && prev[0].sub === r.sub) {
      const p = prev[0]
      const ctx = (p.ctx ?? 0) + (r.ctx ?? 0)
      out[out.length - 1] = [{ ...p, end: r.end ?? p.end, ctx: ctx || undefined,
        shared: p.shared || r.shared || (p.ctx === undefined) !== (r.ctx === undefined) }, prev[1] + 1]
      continue
    }
    out.push([r, 0])
  }
  const running = out.filter(x => x[0].state === 'running')
  const done = out.filter(x => x[0].state !== 'running')
  const keep = running.slice(-Math.max(1, Math.floor(limit / 2)))
  return limit > keep.length ? [...done.slice(-(limit - keep.length)), ...keep] : keep
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    await $.command.register({
      name: 'lot-map',
      description: 'Show or hide the lot map of this session: /lot-map on, /lot-map off, /lot-map toggle',
    })
    if (await read($, isOn)) await $.ui.open({ id: PANE, title: TITLE, backgroundColor: 'transparent' })
    return started
  }).catch(($, e, next) => next(e))

  on('command.run', { command: 'lot-map' }, async ($, e) => {
    const arg = e.args.trim().toLowerCase()
    if (arg !== '' && arg !== 'on' && arg !== 'off' && arg !== 'toggle') {
      return { text: 'Usage: /lot-map [on | off | toggle]' }
    }
    const want = arg === 'off' ? false : arg === 'toggle' ? !(await isOpenNow($)) : true
    await setPane($, want)
    return { text: want ? 'Lot map opened.' : 'Lot map closed.' }
  })

  // The pane closing by any hand ends the motion with it.
  on('ui.close', async ($, e, next) => {
    if (e.id === PANE) {
      stopMotion()
      staged = undefined
    }
    return next(e)
  }).catch(($, e, next) => next(e))

  on('prompt.submit', async ($, e, next) => {
    try {
      if (!e.isMeta && !e.command) {
        const at = await $.clock.now()
        await update($, meta, m => ({ ...m, turns: m.turns + 1, turnStart: at }))
        await event($, 'you', 'your message', 'message')
      }
    } catch (error) {
      await fault($, 'prompt.submit', error)
    }
    return next(e)
  }).catch(($, e, next) => next(e))

  on('tool.call', async ($, e, next) => {
    let id = ''
    let station: Station = 'shell'
    const sub = Boolean(e.agentId)
    try {
      const [s, label] = toolStation(String(e.tool), e as unknown as Record<string, unknown>)
      station = s
      id = newId()
      const at = await $.clock.now()
      await addRow($, { id, station, label, state: 'running', kind: 'call', sub, start: at })
      await tally($, station, { calls: 1 })
      if (!sub) {
        await update($, route, r => (r[r.length - 1] === station ? r : [...r, station].slice(-ROUTE_LEN)))
        await update($, meta, m => ({ ...m, pending: [...m.pending, id] }))
      }
    } catch (error) {
      await fault($, 'tool.call', error)
    }
    const ran = await next(e)
    try {
      if (id) {
        const at = await $.clock.now()
        const text = String(ran.text ?? '').slice(0, 200)
        const refused = ran.deny !== undefined || /^\w+:\w+ hook error/.test(text)
        const state: RowState = refused ? 'refused' : ran.isError ? 'failed' : 'done'
        let ms = 0
        await update($, rows, list => list.map(r => {
          if (r.id !== id) return r
          ms = at - r.start
          return { ...r, state, end: at }
        }))
        await tally($, station, { ms, errors: state === 'done' ? 0 : 1 })
        if (refused) await event($, 'hooks', `refused ${clean(e.tool, 60)}`, 'hook', 'refused')
      }
    } catch (error) {
      await fault($, 'tool.call result', error)
    }
    return ran
  }).catch(($, e, next) => next(e))

  // One model request; the main conversation's carries the context figure.
  on('turn.step', async function* ($, e, next) {
    const result = yield* next(e)
    try {
      if (!e.agentId) {
        const m = await read($, meta)
        const model = result.usage?.model || e.model
        if (m.model && model && model !== m.model) {
          await event($, 'model', `${shortModel(m.model)} → ${shortModel(model)}`, 'switch', 'info')
        }
        await tally($, 'model', { calls: 1 })
        const ctx = result.usage ? promptOf(result.usage) : 0
        if (ctx && m.context && !m.compacted) await spread($, m.pending, ctx - m.context)
        await update($, meta, x => ({
          ...x, model: model || x.model, steps: x.steps + 1, context: ctx || x.context, pending: [], compacted: false,
        }))
      } else {
        await tally($, 'model', { calls: 1 })
      }
    } catch (error) {
      await fault($, 'turn.step', error)
    }
    return result
  })

  on('session.compact', async ($, e, next) => {
    const done = await next(e)
    try {
      if (!e.agentId) {
        await update($, meta, m => ({ ...m, compacted: true, pending: [] }))
        await event($, 'model', 'context compaction', 'compaction')
      }
    } catch (error) {
      await fault($, 'session.compact', error)
    }
    return done
  }).catch(($, e, next) => next(e))

  // What a settings hook added for the model: Hindsight's memory, or another hook's context.
  on('session.append', { door: 'hook-context' }, async ($, e, next) => {
    try {
      if (!e.agentId && e.origin.kind === 'hook') {
        const hook = clean((e.origin as { event?: unknown }).event ?? 'hook', 40)
        const memory = isMemoryText(textOf(e.message.content))
        await event($, memory ? 'memory' : 'hooks', `${hook}: added context`, 'hook')
        if (memory) await tally($, 'memory', { hookMemory: 1 })
      }
    } catch (error) {
      await fault($, 'session.append', error)
    }
    return next(e)
  }).catch(($, e, next) => next(e))

  on('turn.complete', async ($, e, next) => {
    try {
      if (!e.agentId) {
        const m = await read($, meta)
        await event($, 'you', 'answer', 'answer', 'done', m.turnStart || undefined)
      }
    } catch (error) {
      await fault($, 'turn.complete', error)
    }
    return next(e)
  }).catch(($, e, next) => next(e))

  // ============================================================
  // Drawing
  // ============================================================

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const elements = $.ui.resolve(e)
    const { Box, Text } = elements
    const Image = 'Image' in elements ? elements.Image : undefined
    const list = await read($, rows)
    const b = await read($, board)
    const r = await read($, route)
    const m = await read($, meta)
    const nFaults = await read($, faults)
    const width = Math.max(20, e.props.bodyColumns)
    const height = Math.max(8, e.props.scroll?.bodyRows ?? 24)
    const pct = m.context ? roundHalfEven((100 * m.context) / WINDOW) : 0
    const pctColor = pct >= 90 ? 'error' : pct >= 70 ? 'warning' : undefined
    const used = STATIONS.filter(s => b[s].calls > 0)
    const running = list.filter(x => x.state === 'running' && x.kind === 'call')
    // A picture under the new Herdr, which shows Kitty graphics; braille elsewhere.
    // A terminal that cannot show the picture gets the engine's dim alt line instead.
    const canPicture = Image !== undefined && e.surface === 'terminal'
      && (await $.env.get('HC_TERMINAL_ID')) !== undefined
    // The map takes the station table's place (its labels carry each station's calls,
    // errors and context) and as many lines as leave the header, the route, the now
    // line, their margins and the rows under it, at most its own proportion. The
    // picture scales its drawing to any size, so in a short pane it shrinks to 6 lines
    // and keeps one row under it; braille writes its labels into cells and needs 10
    // lines and three rows. MEASURED 2026-11-23: at 18 body rows the picture gave way
    // to the table, which a pane loses when a background agent's line appears below.
    const mapCols = Math.min(76, e.props.bodyColumns)
    const minRows = canPicture ? 6 : 10
    const minList = canPicture ? 1 : 3
    const mapRows = Math.min(Math.max(12, roundHalfEven((mapCols * 21) / 76)), height - (6 + minList))
    // The picture places its labels in pixels and fits from 40 columns; braille
    // writes them into cells and needs 50 before they stop running into each other.
    const isMapShown = e.props.bodyColumns >= (canPicture ? 40 : 50) && mapRows >= minRows
    const isPicture = isMapShown && canPicture
    const room = isMapShown ? Math.max(minList, height - (mapRows + 6)) : Math.max(3, height - used.length - 7)
    const mapFill = m.context ? Math.min(1, m.context / WINDOW) : 0
    // The model works on a turn when the newest of your message and its answer is
    // the message and no call runs: its station is busy then, as in the original.
    const turn = [...list].reverse().find(x => x.station === 'you' && (x.kind === 'message' || x.kind === 'answer'))
    const isThinking = turn?.kind === 'message' && running.length === 0
    const busy = new Set<Station>(running.map(x => x.station))
    if (isThinking) busy.add('model')
    const picture = isPicture ? renderPicture(b, busy, r, mapFill, shortModel(m.model || '?'), mapCols, mapRows) : undefined
    // Motion while a call runs; the terminal's drawing alone decides it, so another
    // surface's drawing never stops or starts it.
    if (e.surface === 'terminal') {
      if (isPicture && busy.size > 0 && !isMotionRefused && Date.now() - lastWork <= STALE_MS) {
        staged = { scene: sceneFor(b, busy, r, mapFill, shortModel(m.model || '?'), mapCols, mapRows), cols: mapCols, rows: mapRows }
        if (!motion) startMotion($, FRAME_MS)
      } else {
        stopMotion()
        staged = undefined
      }
      // A picture that left the pane comes back with a fresh chance to move.
      if (!isPicture) isMotionRefused = false
    }
    const mapLines = isMapShown && !isPicture ? renderBraille(b, busy, mapFill, shortModel(m.model || '?'), mapCols, mapRows) : []
    const toneProps = (t: Tone): { color?: string; dimColor?: boolean; bold?: boolean } =>
      t === 'error' ? { color: 'error' } : t === 'warning' ? { color: 'warning' }
        : t === 'dim' ? { dimColor: true } : t === 'bold' ? { bold: true } : {}
    const shown = fold(list, room)
    const cell = (s: string, n: number): string => (s.length >= n ? s.slice(0, n) : s + ' '.repeat(n - s.length))
    const right = (s: string, n: number): string => (s.length >= n ? s : ' '.repeat(n - s.length) + s)

    return (
      <Box flexDirection="column">
        <Text wrap="truncate-end">
          <Text bold>{shortModel(m.model || '?')}</Text>
          <Text>{'  context '}</Text>
          <Text color={pctColor}>{m.context ? `${pct}%` : '?'}</Text>
          <Text dimColor>{`  steps ${m.steps}  turns ${m.turns}`}</Text>
        </Text>
        {!isMapShown && used.length === 0 && <Text dimColor>Nothing yet: the map fills as Claude works.</Text>}
        {!isMapShown && used.length > 0 && (
          <Text dimColor wrap="truncate-end">{`${cell('', 9)}${right('calls', 6)}${right('err', 5)}${right('time', 9)}${right('ctx', 7)}`}</Text>
        )}
        {!isMapShown && used.map(s => (
          <Text key={s} wrap="truncate-end">
            <Text bold>{cell(NAMES[s], 9)}</Text>
            <Text>{right(String(b[s].calls), 6)}</Text>
            <Text color={b[s].errors ? 'error' : undefined}>{right(b[s].errors ? String(b[s].errors) : '', 5)}</Text>
            <Text dimColor>{right(s === 'you' || s === 'model' || s === 'hooks' ? '' : dur(b[s].ms || undefined), 9)}</Text>
            <Text dimColor>{right(k(b[s].ctx), 7)}</Text>
            {b[s].hookMemory > 0 && <Text dimColor>{`  ↓${b[s].hookMemory}`}</Text>}
          </Text>
        ))}
        {picture && Image && (
          <Box marginTop={1}>
            <Image key="map" source={picture} columns={mapCols} rows={mapRows}
              alt={`lot map: ${used.length} stations, ${roundHalfEven(100 * mapFill)}% context`} />
          </Box>
        )}
        {isMapShown && !isPicture && (
          <Box marginTop={1} flexDirection="column">
            {mapLines.map((line, i) => (
              <Text key={`map-${i}`} wrap="truncate-end">
                {line.length ? line.map((run, j) => <Text key={String(j)} {...toneProps(run.tone)}>{run.text}</Text>) : ' '}
              </Text>
            ))}
          </Box>
        )}
        <Box marginTop={1}>
          <Text wrap="truncate-end" dimColor>{`route: ${r.map(s => NAMES[s]).join(' → ') || '-'}`}</Text>
        </Box>
        <Text wrap="truncate-end" bold={running.length > 0 || isThinking}>
          {running.length ? running.slice(-3).map(x => `${NAMES[x.station]}: ${x.label}`).join(', ')
            : isThinking ? `Model: ${shortModel(m.model || '?')} · step ${m.steps + 1}` : 'Nothing is running.'}
        </Text>
        <Box marginTop={1} flexDirection="column">
          {shown.map(([x, extra]) => {
            const tail = x.state === 'running' ? 'running'
              : `${x.kind === 'call' || x.kind === 'answer' || x.kind === 'hook' ? (x.end === undefined ? '-' : dur(x.end - x.start)) : '-'}${x.ctx ? ` ${k(x.ctx, x.shared)}` : ''}`
            const label = `${x.sub ? '↳ ' : ''}${x.label}${extra ? ` +${extra}` : ''}`
            // The original's colours: running blue, failed or refused red, a model
            // switch amber, the rest dim grey.
            const rowColor = x.state === 'failed' || x.state === 'refused' ? 'error'
              : x.state === 'running' ? 'suggestion' : x.kind === 'switch' ? 'warning' : undefined
            const isDim = rowColor === undefined
            return (
              <Box key={x.id} flexDirection="row">
                <Text color={rowColor} dimColor={isDim}>{`${MARK[x.state]} `}</Text>
                <Box width={9} flexShrink={0}><Text color={rowColor} dimColor={isDim}>{NAMES[x.station]}</Text></Box>
                <Box flexGrow={1} flexShrink={1}><Text color={rowColor} dimColor={isDim} wrap="truncate-end">{label}</Text></Box>
                <Text color={rowColor} dimColor={isDim}>{` ${width >= 40 ? tail : ''}`}</Text>
              </Box>
            )
          })}
        </Box>
        {nFaults > 0 && <Text dimColor>{`lot-map: ${nFaults} fault(s) inside the mod, none reached a call; see the debug log`}</Text>}
      </Box>
    )
  })
}
