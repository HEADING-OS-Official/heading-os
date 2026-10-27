export type RunStatus = 'running' | 'done' | 'failed' | 'stopped'

export type AgentRun = {
  agentId: string
  type: string
  description: string
  model: string
  /** The effort the agent's requests carried, as `turn.step` reported it; absent when none did. */
  effort?: string
  status: RunStatus
  startedAt: number
  endedAt?: number
  /** Input side of the latest request: uncached, cache-read and cache-written tokens. */
  contextTokens: number
  /** The model's context window from our table; null when the table does not know it. */
  contextWindow: number | null
  /** Every token of every request, all four kinds. */
  tokens: number
  /** Estimated dollars of the requests that had a price. */
  costUsd: number
  /** Requests whose model had no price in the table, so their cost is not counted. */
  unpricedRequests: number
  requests: number
  /** Reported by the agent itself through the report_step tool. */
  stepDone?: number
  stepTotal?: number
  stepNote?: string
}

export type Totals = {
  tokens: number
  costUsd: number
  unpricedRequests: number
}

declare module 'claude-code' {
  interface PluginState {
    'agent-watch': {
      agents: AgentRun[]
      main: Totals
      now: number
      /** When the current batch began: the first spawn while nothing was running. */
      batchStartedAt: number
      /** The main conversation's current turn: from the prompt to its `turn.complete`. */
      turn: { startedAt: number; endedAt?: number }
      isFinishedOpen: boolean
    }
  }
}
