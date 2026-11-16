declare module 'claude-code' {
  interface PluginState {
    'context-bar': {
      isBandOn: boolean
      figures: SessionContextBreakdown | null
      window: number | null
      tokens: number | null
      history: { total: number; messages: number }[]
    }
  }
}
