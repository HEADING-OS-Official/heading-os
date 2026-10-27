// agent-watch: a band above the prompt and a panel listing this session's
// subagents, with an estimated cost. Reads only what the engine hands its hooks:
// no network, no model calls, no files, no processes.
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { AgentRun, RunStatus, Totals } from '../types'

// The command, the tool and the hook matchers name themselves in literals so
// `claude plugin validate` can read what the module answers.
const PANE = 'agent-watch'
const MAX_RUNS = 200

const agents = atom({ plugin: 'agent-watch', key: 'agents' } as const, [])
const main = atom({ plugin: 'agent-watch', key: 'main' } as const, { tokens: 0, costUsd: 0, unpricedRequests: 0 })
const now = atom({ plugin: 'agent-watch', key: 'now' } as const, 0)
const batchStartedAt = atom({ plugin: 'agent-watch', key: 'batchStartedAt' } as const, 0)
const turn = atom({ plugin: 'agent-watch', key: 'turn' } as const, { startedAt: 0 })
const isFinishedOpen = atom({ plugin: 'agent-watch', key: 'isFinishedOpen' } as const, true)

// ============================================================
// Prices and context windows
// ============================================================

// USD per million tokens: base input, 5-minute cache write, cache read, output.
// Source: https://platform.claude.com/docs/en/about-claude/pricing, read
// 2026-10-10 (Anthropic first-party API, global routing). Not applied, because a
// hook cannot see them: fast mode, US-only inference (1.1x), batch, and 1-hour
// cache writes (2x input; the usage a hook gets does not split writes by TTL,
// so every write is priced at the 5-minute rate).
type Rates = { input: number; write: number; read: number; output: number }
type Price = { rates: Rates; over100k?: Rates }

const r = (input: number, write: number, read: number, output: number): Rates => ({ input, write, read, output })

const PRICES: Record<string, Price> = {
  'claude-fable-5-1': { rates: r(10, 12.5, 0.25, 50) },
  'claude-mythos-5-1': { rates: r(10, 12.5, 0.25, 50) },
  'claude-fable-5': { rates: r(10, 12.5, 1, 50) },
  'claude-mythos-5': { rates: r(10, 12.5, 1, 50) },
  'claude-opus-5-5': { rates: r(4, 5, 0.2, 20) },
  'claude-opus-5': { rates: r(5, 6.25, 0.5, 25) },
  'claude-opus-4-8': { rates: r(5, 6.25, 0.5, 25) },
  'claude-opus-4-7': { rates: r(5, 6.25, 0.5, 25) },
  'claude-opus-4-6': { rates: r(5, 6.25, 0.5, 25) },
  'claude-opus-4-5': { rates: r(5, 6.25, 0.5, 25) },
  'claude-opus-4-1': { rates: r(15, 18.75, 1.5, 75) },
  'claude-opus-4': { rates: r(15, 18.75, 1.5, 75) },
  'claude-sonnet-5-5': { rates: r(2, 2.5, 0.1, 10) },
  'claude-sonnet-5': { rates: r(2, 2.5, 0.2, 10) },
  'claude-sonnet-4-6': { rates: r(3, 3.75, 0.3, 15) },
  'claude-sonnet-4-5': { rates: r(3, 3.75, 0.3, 15) },
  'claude-sonnet-4': { rates: r(3, 3.75, 0.3, 15) },
  // Priced per request by prompt length: a prompt over 100,000 tokens, cache
  // reads and writes included, pays the second set.
  'claude-haiku-5-5': { rates: r(0.1, 0.125, 0.01, 0.5), over100k: r(0.5, 0.625, 0.05, 2.5) },
  'claude-haiku-4-5': { rates: r(1, 1.25, 0.1, 5) },
  'claude-3-5-haiku': { rates: r(0.8, 1, 0.08, 4) },
}

// Tokens. Source: the official models overview, read 2026-10-10, for the
// current four; the claude-api skill's model table (cached 2026-09-25) for the
// older ones. A model in neither shows its tokens without a percentage.
const WINDOWS: Record<string, number> = {
  'claude-fable-5-1': 1_000_000,
  'claude-mythos-5-1': 1_000_000,
  'claude-fable-5': 1_000_000,
  'claude-opus-5-5': 1_000_000,
  'claude-opus-5': 1_000_000,
  'claude-opus-4-8': 1_000_000,
  'claude-opus-4-7': 1_000_000,
  'claude-opus-4-6': 1_000_000,
  'claude-sonnet-5-5': 1_000_000,
  'claude-sonnet-5': 1_000_000,
  'claude-sonnet-4-6': 1_000_000,
  'claude-haiku-5-5': 1_000_000,
  'claude-haiku-4-5': 200_000,
}

const KEYS = Object.keys(PRICES).concat(Object.keys(WINDOWS)).filter((k, i, all) => all.indexOf(k) === i)
  .sort((a, b) => b.length - a.length)

// The table key a reported model id names, or null. Accepts a date suffix and a
// provider's wrapping (`anthropic.`, `@...`, `-v1:0`, `[1m]`), and refuses a
// longer version: `claude-opus-5-5` never matches `claude-opus-5`.
const keyOf = (model: string): string | null => {
  const m = model.toLowerCase()
  for (const k of KEYS) {
    const at = m.indexOf(k)
    if (at < 0 || (at > 0 && /[a-z0-9]/.test(m[at - 1] ?? ''))) continue
    const rest = m.slice(at + k.length).replace(/^-\d{8}/, '')
    if (rest === '' || !/^[0-9]|^-[0-9]/.test(rest)) return k
  }
  return null
}

type Usage = {
  input_tokens: number
  output_tokens: number
  cache_read_input_tokens: number
  cache_creation_input_tokens: number
}

const promptOf = (u: Usage): number =>
  (u.input_tokens || 0) + (u.cache_read_input_tokens || 0) + (u.cache_creation_input_tokens || 0)

const allOf = (u: Usage): number => promptOf(u) + (u.output_tokens || 0)

// Dollars for one request, or null when the model has no price. `isSummed`
// marks a turn's total rather than one request: a length-tiered model can then
// be priced only when even the whole sum stays under the threshold.
const costOf = (model: string, u: Usage, isSummed = false): number | null => {
  const k = keyOf(model)
  const price = k ? PRICES[k] : undefined
  if (!price) return null
  let rates = price.rates
  if (price.over100k && promptOf(u) > 100_000) {
    if (isSummed) return null
    rates = price.over100k
  }
  return (
    (u.input_tokens || 0) * rates.input +
    (u.cache_creation_input_tokens || 0) * rates.write +
    (u.cache_read_input_tokens || 0) * rates.read +
    (u.output_tokens || 0) * rates.output
  ) / 1_000_000
}

const windowOf = (model: string): number | null => {
  const k = keyOf(model)
  return (k && WINDOWS[k]) || null
}

// ============================================================
// Formatting
// ============================================================

// How long the band stays above the prompt after the last agent of a batch ends.
const LINGER_MS = 30_000

const money = (usd: number): string => (usd > 0 && usd < 0.01 ? '<$0.01' : `$${usd.toFixed(2)}`)

// "≈$0.12"; a trailing "+" when some requests had no price; "no price" when none had.
const approx = (usd: number, unpriced: number): string =>
  unpriced === 0 ? `≈${money(usd)}` : usd > 0 ? `≈${money(usd)}+` : 'no price'

const tokens = (n: number): string =>
  n >= 1_000_000 ? `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1)}M` : n >= 1000 ? `${Math.round(n / 1000)}k` : `${n}`

// m:ss, or h:mm:ss from an hour.
const clock = (ms: number): string => {
  const s = Math.max(0, Math.floor(ms / 1000))
  const m = Math.floor(s / 60)
  const ss = String(s % 60).padStart(2, '0')
  return m < 60 ? `${m}:${ss}` : `${Math.floor(m / 60)}:${String(m % 60).padStart(2, '0')}:${ss}`
}

// "Opus 5.5" for a model the tables know; the reported id otherwise.
const modelName = (model: string): string => {
  const k = keyOf(model)
  const m = k ? /^claude-([a-z]+)-(\d+)(?:-(\d+))?$/.exec(k) : null
  if (!m) return model
  const family = m[1] ?? ''
  return `${family.charAt(0).toUpperCase()}${family.slice(1)} ${m[2]}${m[3] ? `.${m[3]}` : ''}`
}

const percentOf = (a: AgentRun): number | null =>
  a.contextWindow ? Math.min(100, Math.round((a.contextTokens / a.contextWindow) * 100)) : null

// What a row's bar shows: the agent's own steps when it reported them, full
// once it answered, otherwise how full its context is.
const fractionOf = (a: AgentRun): number => {
  if (a.stepTotal) return Math.min(1, (a.stepDone ?? 0) / a.stepTotal)
  if (a.status === 'done') return 1
  return a.contextWindow ? Math.min(1, a.contextTokens / a.contextWindow) : 0
}

// The line under a row's title: steps if reported, context, cost, elapsed.
const metricsOf = (a: AgentRun, at: number): string => {
  const parts: string[] = []
  if (a.stepTotal) parts.push(`step ${a.stepDone ?? 0}/${a.stepTotal}${a.stepNote ? ` ${a.stepNote}` : ''}`)
  const pct = percentOf(a)
  if (a.contextTokens === 0) parts.push(a.status === 'running' ? 'starting' : 'ctx n/a')
  else parts.push(pct === null ? `ctx ${tokens(a.contextTokens)}` : `ctx ${pct}% · ${tokens(a.contextTokens)}`)
  parts.push(approx(a.costUsd, a.unpricedRequests))
  parts.push(clock((a.endedAt ?? at) - a.startedAt))
  return parts.join(' · ')
}

const MARK: Record<RunStatus, string> = { running: '●', done: '✓', failed: '✕', stopped: '■' }
const COLOR: Record<RunStatus, 'suggestion' | 'success' | 'error' | 'inactive'> = {
  running: 'suggestion',
  done: 'success',
  failed: 'error',
  stopped: 'inactive',
}

// A bar drawn with box-drawing glyphs: heavy for the done part, light for the rest.
const BAR_ON = '━'
const BAR_OFF = '─'
const cellsOf = (fraction: number, width: number): [number, number] => {
  const w = Math.max(0, Math.floor(width))
  const on = fraction > 0 ? Math.min(w, Math.max(1, Math.round(fraction * w))) : 0
  return [on, w - on]
}

const sum = (list: AgentRun[]): Totals => ({
  tokens: list.reduce((n, a) => n + a.tokens, 0),
  costUsd: list.reduce((n, a) => n + a.costUsd, 0),
  unpricedRequests: list.reduce((n, a) => n + a.unpricedRequests, 0),
})

const lastEndOf = (list: AgentRun[]): number => Math.max(0, ...list.map(a => a.endedAt ?? 0))

// ============================================================
// State changes
// ============================================================

const togglePane = async ($: EngineInterface): Promise<boolean> => {
  const isOpen = (await $.ui.panes()).some(p => p.id === PANE)
  if (isOpen) {
    await $.ui.close({ id: PANE })
    return false
  }
  await $.ui.open({ id: PANE, title: 'Agents' })
  return true
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    await $.command.register({
      name: 'agent-watch',
      description: 'Show or hide the panel of subagents: model, progress, elapsed time and estimated cost',
    })
    await $.tool.register({
      name: 'report_step',
      description:
        'For subagents only: report progress on your own task to the agent panel the person watches. ' +
        'After planning, call it with `total` (your plan in 3 to 8 steps) and `done: 0`; call it again as each step finishes. ' +
        'Optional, silent and cheap: it only updates a progress line.',
      inputSchema: {
        type: 'object',
        properties: {
          done: { type: 'integer', minimum: 0, description: 'Steps finished so far.' },
          total: { type: 'integer', minimum: 1, description: 'Steps planned; may change when the plan changes.' },
          note: { type: 'string', description: 'The step in progress, a few words.' },
        },
        required: ['done'],
      },
    })
    // Moves the clocks once a second, and only while something needs it: an
    // agent runs, the band is lingering after a batch, or the main turn runs.
    $.clock.every(1000, () => {
      void (async () => {
        const list = await read($, agents)
        const t = await read($, turn)
        const at = await $.clock.now()
        const isNeeded =
          list.some(a => a.status === 'running') ||
          (list.length > 0 && at - lastEndOf(list) < LINGER_MS + 1000) ||
          (t.startedAt > 0 && t.endedAt === undefined)
        if (isNeeded) await update($, now, () => at)
      })()
    })
    return started
  })

  on('command.run', { command: 'agent-watch' }, async $ => {
    const isOpen = await togglePane($)
    return { text: isOpen ? 'Agent panel opened.' : 'Agent panel closed.' }
  })

  // The main conversation's turn starts with the person's prompt.
  on('prompt.submit', async ($, e, next) => {
    const at = await $.clock.now()
    await update($, turn, () => ({ startedAt: at }))
    await update($, now, () => at)
    return next(e)
  }).catch(($, e, next) => next(e))

  on('agent.spawn', async ($, e, next) => {
    const started = await next(e)
    if (started.deny !== undefined || !started.agentId) return started
    const agentId = started.agentId
    const at = await $.clock.now()
    const run: AgentRun = {
      agentId,
      type: e.subagentType,
      description: e.description,
      model: started.model,
      status: 'running',
      startedAt: at,
      contextTokens: 0,
      contextWindow: windowOf(started.model),
      tokens: 0,
      costUsd: 0,
      unpricedRequests: 0,
      requests: 0,
    }
    const list = await read($, agents)
    if (!list.some(a => a.status === 'running')) await update($, batchStartedAt, () => at)
    await update($, agents, all => [...all.filter(a => a.agentId !== agentId), run].slice(-MAX_RUNS))
    await update($, now, () => at)
    return started
  }).catch(($, e, next) => next(e))

  // One model request: the subagent's, or the main conversation's.
  on('turn.step', async function* ($, e, next) {
    const result = yield* next(e)
    const usage = result.usage
    if (!usage) return result
    const model = usage.model || e.model
    const cost = costOf(model, usage)
    const agentId = e.agentId
    if (!agentId) {
      await update($, main, t => ({
        tokens: t.tokens + allOf(usage),
        costUsd: t.costUsd + (cost ?? 0),
        unpricedRequests: t.unpricedRequests + (cost === null ? 1 : 0),
      }))
      return result
    }
    await update($, agents, list =>
      list.map(a =>
        a.agentId !== agentId
          ? a
          : {
              ...a,
              model,
              effort: e.effort === undefined ? a.effort : String(e.effort),
              contextTokens: promptOf(usage),
              contextWindow: windowOf(model),
              tokens: a.tokens + allOf(usage),
              costUsd: a.costUsd + (cost ?? 0),
              unpricedRequests: a.unpricedRequests + (cost === null ? 1 : 0),
              requests: a.requests + 1,
            },
      ),
    )
    return result
  })

  on('turn.complete', async ($, e, next) => {
    const agentId = e.agentId
    const at = await $.clock.now()
    if (!agentId) {
      await update($, turn, t => (t.startedAt > 0 && t.endedAt === undefined ? { ...t, endedAt: at } : t))
      await update($, now, () => at)
      return next(e)
    }
    const status: RunStatus = e.reason === 'answer' ? 'done' : e.reason === 'aborted' ? 'stopped' : 'failed'
    const usage = e.usage
    await update($, agents, list =>
      list.map(a => {
        if (a.agentId !== agentId) return a
        // No request was seen (the mod reloaded mid-run, say): take the turn's sum.
        if (a.requests > 0 || !usage) return { ...a, status, endedAt: at }
        const model = usage.model || a.model
        const cost = costOf(model, usage, true)
        return {
          ...a,
          status,
          endedAt: at,
          model,
          tokens: allOf(usage),
          costUsd: cost ?? 0,
          unpricedRequests: cost === null ? 1 : 0,
          requests: 1,
        }
      }),
    )
    await update($, now, () => at)
    return next(e)
  })

  on('tool.call', { tool: 'mcp__agent-watch__report_step' }, async ($, e) => {
    const agentId = e.agentId
    if (!agentId) return { result: 'ignored: only a subagent reports steps' }
    const input = e as unknown as { done?: unknown; total?: unknown; note?: unknown }
    const done = typeof input.done === 'number' ? Math.max(0, Math.round(input.done)) : undefined
    const total = typeof input.total === 'number' ? Math.max(0, Math.round(input.total)) : undefined
    const note = typeof input.note === 'string' ? input.note.trim().slice(0, 80) : undefined
    let isKnown = false
    await update($, agents, list =>
      list.map(a => {
        if (a.agentId !== agentId) return a
        isKnown = true
        const stepTotal = total ?? a.stepTotal
        const stepDone = done ?? a.stepDone ?? 0
        return {
          ...a,
          stepTotal,
          stepDone: stepTotal ? Math.min(stepTotal, stepDone) : stepDone,
          stepNote: note || undefined,
        }
      }),
    )
    return { result: isKnown ? 'ok' : 'ignored: this agent started before the panel was loaded' }
  }).catch(() => ({ result: 'ignored: the panel could not record this step' }))

  // ============================================================
  // Drawing
  // ============================================================

  // The band: one row for the current batch, kept LINGER_MS after it ends.
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey) return next(e)
    const start = await read($, batchStartedAt)
    const batch = (await read($, agents)).filter(a => a.startedAt >= start)
    if (batch.length === 0) return next(e)
    const running = batch.filter(a => a.status === 'running')
    const lastEnd = lastEndOf(batch)
    const at = Math.max(await read($, now), lastEnd, ...batch.map(a => a.startedAt))
    if (running.length === 0 && at - lastEnd >= LINGER_MS) return next(e)
    const { Box, Text, Button } = $.ui.resolve(e)
    const finished = batch.length - running.length
    const failed = batch.filter(a => a.status === 'failed').length
    const since = Math.min(...batch.map(a => a.startedAt))
    const t = sum(batch)
    // Share of the batch finished; a running agent that reported steps counts its share.
    const share =
      batch.reduce((n, a) => n + (a.status !== 'running' ? 1 : a.stepTotal ? Math.min(1, (a.stepDone ?? 0) / a.stepTotal) : 0), 0) /
      batch.length
    const mark = running.length > 0 ? MARK.running : failed > 0 ? MARK.failed : MARK.done
    const counts = running.length > 0 ? `${running.length} running · ${finished} finished` : `${finished} finished`
    const failedText = failed > 0 ? ` · ${failed} failed` : ''
    const right = `${approx(t.costUsd, t.unpricedRequests)} · ${clock((running.length > 0 ? at : lastEnd) - since)}`
    // `agents ` + mark + counts, two spaces either side of the bar, the button `[ panel ]`.
    const used = 7 + 2 + counts.length + failedText.length + 2 + 2 + right.length + 2 + 9
    const barWidth = Math.min(30, e.props.bodyColumns - used)
    const [filled, empty] = cellsOf(share, barWidth)
    const barColor = running.length > 0 ? 'suggestion' : failed > 0 ? 'error' : 'success'
    return (
      <Box>
        <Text wrap="truncate-end">
          <Text bold>agents </Text>
          <Text color={running.length > 0 ? 'suggestion' : failed > 0 ? 'error' : 'success'}>
            {mark} {counts}
          </Text>
          {failed > 0 && <Text color="error">{failedText}</Text>}
          {barWidth >= 6 && <Text>{'  '}</Text>}
          {barWidth >= 6 && <Text color={barColor}>{BAR_ON.repeat(filled)}</Text>}
          {barWidth >= 6 && <Text dimColor>{BAR_OFF.repeat(empty)}</Text>}
          <Text dimColor>
            {'  '}
            {right}
            {'  '}
          </Text>
        </Text>
        <Button key="panel" label="panel" onPress={() => void togglePane($)} />
      </Box>
    )
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const list = await read($, agents)
    const m = await read($, main)
    const t = await read($, turn)
    const isOpen = await read($, isFinishedOpen)
    const at = Math.max(await read($, now), ...list.map(a => a.endedAt ?? a.startedAt), 0)
    const width = Math.max(10, e.props.bodyColumns)
    const subs = sum(list)
    const session: Totals = {
      tokens: subs.tokens + m.tokens,
      costUsd: subs.costUsd + m.costUsd,
      unpricedRequests: subs.unpricedRequests + m.unpricedRequests,
    }
    const running = list.filter(a => a.status === 'running')
    const finished = list.filter(a => a.status !== 'running')
    const failed = finished.filter(a => a.status === 'failed').length

    const tiles: [string, string][] = [
      ['Session cost', approx(session.costUsd, session.unpricedRequests)],
      ['Session tokens', tokens(session.tokens)],
      ['This turn', t.startedAt > 0 ? clock((t.endedAt ?? at) - t.startedAt) : 'n/a'],
    ]
    // Three framed tiles side by side when each gets 14 cells; one line otherwise.
    const tileWidth = Math.floor((width - 2) / 3)

    const row = (a: AgentRun) => {
      const [filled, empty] = cellsOf(fractionOf(a), width)
      return (
        <Box key={a.agentId} flexDirection="column" marginTop={1}>
          <Box flexDirection="row">
            <Box flexGrow={1} flexShrink={1}>
              <Text bold wrap="truncate-end">
                {a.description || '(no description)'}
              </Text>
            </Box>
            <Text color={COLOR[a.status]}> {MARK[a.status]}</Text>
          </Box>
          <Text wrap="truncate-end">
            <Text color="suggestion">{a.type}</Text>
            <Text dimColor>
              {' '}
              {modelName(a.model)}
              {a.effort ? ` · ${a.effort}` : ''}
            </Text>
          </Text>
          <Box flexDirection="row" justifyContent="flex-end">
            <Text dimColor wrap="truncate-end">
              {metricsOf(a, at)}
            </Text>
          </Box>
          <Text wrap="truncate-end">
            <Text color={COLOR[a.status]}>{BAR_ON.repeat(filled)}</Text>
            <Text dimColor>{BAR_OFF.repeat(empty)}</Text>
          </Text>
        </Box>
      )
    }

    return (
      <Box flexDirection="column">
        {tileWidth >= 14 ? (
          <Box flexDirection="row">
            {tiles.map(([label, value], i) => (
              <Box
                key={label}
                width={tileWidth}
                marginRight={i < tiles.length - 1 ? 1 : 0}
                flexDirection="column"
                borderStyle="round"
                borderDimColor
                paddingX={1}
              >
                <Text dimColor wrap="truncate-end">
                  {label}
                </Text>
                <Text bold wrap="truncate-end">
                  {value}
                </Text>
              </Box>
            ))}
          </Box>
        ) : (
          <Text wrap="truncate-end">{tiles.map(([label, value]) => `${label} ${value}`).join(' · ')}</Text>
        )}
        {list.length === 0 && (
          <Box marginTop={1}>
            <Text dimColor>No subagents in this session yet.</Text>
          </Box>
        )}
        {running.length > 0 && (
          <Box marginTop={1}>
            <Text bold>Running · {running.length}</Text>
          </Box>
        )}
        {running.map(row)}
        {finished.length > 0 && (
          <Box marginTop={1}>
            <Button
              key="finished"
              plain
              label={`${isOpen ? '▾' : '▸'} Finished · ${finished.length}${failed > 0 ? ` (${failed} failed)` : ''}`}
              onPress={() => void update($, isFinishedOpen, v => !v)}
            />
          </Box>
        )}
        {isOpen && finished.map(row)}
        <Box marginTop={1}>
          <Text wrap="truncate-end" dimColor>
            Subagents {tokens(subs.tokens)} · {approx(subs.costUsd, subs.unpricedRequests)}
            {'   '}Main {tokens(m.tokens)} · {approx(m.costUsd, m.unpricedRequests)}
          </Text>
        </Box>
        <Text dimColor wrap="wrap">
          ≈ is an estimate from token counts and API list prices of 2026-10-10, counted since this panel loaded. Not a bill: 1-hour cache writes, fast mode and regional pricing are not applied.
          {session.unpricedRequests > 0 ? ` A + means ${session.unpricedRequests} request(s) had a model with no price and are not counted.` : ''}
        </Text>
      </Box>
    )
  })
}
