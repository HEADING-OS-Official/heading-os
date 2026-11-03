declare module 'claude-code' {
  interface PluginState {
    'context-bar': {
      isBandOn: boolean
      figures: SessionContextBreakdown | null
    }
  }
}
