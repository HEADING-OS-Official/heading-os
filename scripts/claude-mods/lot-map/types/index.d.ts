export type Station =
  | 'you' | 'model' | 'memory' | 'files' | 'shell'
  | 'network' | 'browser' | 'agents' | 'skills' | 'hooks'

export type RowState = 'running' | 'done' | 'failed' | 'refused' | 'info'
export type RowKind = 'call' | 'message' | 'answer' | 'hook' | 'switch' | 'compaction'

/** One row of the list under the board. `label` holds a name, never content. */
export type Row = {
  id: string
  station: Station
  label: string
  state: RowState
  kind: RowKind
  /** Made by a subagent. */
  sub: boolean
  start: number
  end?: number
  /** Tokens the context grew by with this call's answer; `shared` when an even share of one step's growth. */
  ctx?: number
  shared?: boolean
}

export type Tally = { calls: number; errors: number; ms: number; ctx: number; hookMemory: number }
export type Board = Record<Station, Tally>

export type Meta = {
  model: string
  /** Input side of the main conversation's latest request: uncached, cache-read and cache-written. */
  context: number
  steps: number
  turns: number
  turnStart: number
  /** The main loop's calls since its last model step: their answers go with the next step. */
  pending: string[]
  /** A compaction since the last step: the next step's growth goes nowhere. */
  compacted: boolean
}

declare module 'claude-code' {
  interface PluginState {
    'lot-map': {
      rows: Shaped<Row[]>
      board: Shaped<Board>
      route: Shaped<Station[]>
      meta: Shaped<Meta>
      /** Whether the person switched the pane on; a reload reopens it. */
      isOn: boolean
      /** Faults inside the mod, shown in the pane's footer; none ever reaches a call or a turn. */
      faults: number
    }
  }
}
