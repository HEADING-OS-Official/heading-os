import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

const PANE = {
  plugin: 'lot-map',
  component: 'Pane',
  requestId: 'lot-map',
  props: {
    title: 'Lot map',
    isFocused: false,
    bodyColumns: 80,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: 30 },
    view: {},
  },
} as const

// The rest of a typed /lot-map, as the engine hands it to command.run.
const RUN = {
  command: 'lot-map',
  origin: { kind: 'composer' },
  presentation: { isFullscreen: true, columns: 120 },
} as const

// The engine's clock and log, which nothing beneath the mod answers in a test.
// A log line is a fault inside the mod: kept so a test can fail on it.
const engine = (on: On, logs: string[]) => {
  let t = 1_000
  on('clock.now', () => ({ value: (t += 250) }))
  on('ui.log', (_, e) => { logs.push(String(e.text)); return { value: undefined } })
  on('command.register', () => ({ value: { command: 'lot-map' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  mock.env(on, {}) // no Herdr variable: the map is braille
}

test('a file-only shell line lights Files, any other command Shell, on every surface', async ($, on) => {
  // Nothing sits beneath the mod: each hook here stands for the engine.
  const logs: string[] = []
  engine(on, logs)
  on('tool.call', () => ({ result: {} }))

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await $.tool.call({ tool: 'Bash', command: 'grep -n "a\\|b" notes.txt | head', description: 'Search the notes' })
  await $.tool.call({ tool: 'Bash', command: 'cd /x; git status' })

  for (const surface of ['terminal', 'desktop', 'vscode', 'mobile'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect((await ui.find({ type: 'Text', text: /^Files$/ }))?.text).toBe('Files')
    expect((await ui.find({ type: 'Text', text: /Search the notes/ }))?.text).toBe('Search the notes')
    expect((await ui.find({ type: 'Text', text: /^git$/ }))?.text).toBe('git')
    await ui.unmount()
  }
  expect(logs).toEqual([])
})

test('a call a hook refused shows ✕ and a Hooks row', async ($, on) => {
  const logs: string[] = []
  engine(on, logs)
  on('tool.call', () => ({ deny: 'refused by a test hook' }))

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await $.tool.call({ tool: 'Read', file_path: '/a/b/c.md' })

  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Text', text: /refused Read/ }))?.text).toBe('refused Read')
  expect((await ui.findAll({ type: 'Text', text: /^✕ $/ })).length).toBe(2)
  await ui.unmount()
  expect(logs).toEqual([])
})

test('/lot-map off closes the pane and /lot-map on opens it', async ($, on) => {
  const logs: string[] = []
  engine(on, logs)
  const opened: string[] = []
  const closed: string[] = []
  let isOpen = true
  on('ui.open', (_, e) => { opened.push(e.id); isOpen = true; return { value: { isPlaced: true } } })
  on('ui.close', (_, e) => { closed.push(e.id); isOpen = false; return { value: undefined } })
  on('ui.panes', () => ({ value: isOpen ? [{ id: 'lot-map', title: 'Lot map', isShown: true, isFocused: false, isPlaced: true }] : [] }))

  const off = await $.command.run({ ...RUN, args: 'off' })
  expect(off.text).toBe('Lot map closed.')
  expect(closed).toEqual(['lot-map'])
  const again = await $.command.run({ ...RUN, args: 'on' })
  expect(again.text).toBe('Lot map opened.')
  expect(opened).toEqual(['lot-map'])
  expect(logs).toEqual([])
})

test('a control character in a model-chosen path never reaches the pane', async ($, on) => {
  const logs: string[] = []
  engine(on, logs)
  on('tool.call', () => ({ result: {} }))

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await $.tool.call({ tool: 'Read', file_path: '/x/\u001b]52;c;SGk=\u0007evil.md' })

  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  const row = await ui.find({ type: 'Text', text: /evil\.md/ })
  expect(row?.text).toContain('evil.md')
  expect(/[\u0000-\u001f\u007f-\u009f]/.test(row?.text ?? '')).toBe(false)
  await ui.unmount()
  expect(logs).toEqual([])
})
