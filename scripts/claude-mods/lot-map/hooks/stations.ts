// lot-map: which station a tool call lights, and the label its row carries.
//
// Ported from scripts/lot-map.py, which holds the same rules for past sessions
// read from a transcript; a rule changed here is changed there too. Both are a
// rebuild of the lot-map mod (github.com/alexeyw/lot-map) from its README alone,
// read 2026-11-22: no line of its code was read or copied.
//
// A label holds a NAME, never content: a file is its last two path parts, a web
// address its host, a shell command the description Claude wrote for it or the
// command's name, a search the tool's name. Command lines, queries and file text
// are read here to choose a station and are never kept.

import type { Station } from '../types'

export type { Station }

export const STATIONS: readonly Station[] = [
  'you', 'model', 'memory', 'files', 'shell', 'network', 'browser', 'agents', 'skills', 'hooks',
]

export const NAMES: Record<Station, string> = {
  you: 'You', model: 'Model', memory: 'Memory', files: 'Files', shell: 'Shell',
  network: 'Network', browser: 'Browser', agents: 'Agents', skills: 'Skills', hooks: 'Hooks',
}

const YOU_TOOLS = new Set(['AskUserQuestion', 'TodoWrite', 'TaskCreate', 'TaskUpdate', 'TaskList',
  'TaskGet', 'ExitPlanMode', 'EnterPlanMode'])
const FILE_TOOLS = new Set(['Read', 'Write', 'Edit', 'MultiEdit', 'NotebookEdit', 'NotebookRead',
  'Grep', 'Glob', 'LSP'])
const SHELL_TOOLS = new Set(['Bash', 'PowerShell'])
const AGENT_TOOLS = new Set(['Agent', 'Task', 'Workflow', 'ScheduleWakeup', 'CronCreate', 'CronDelete',
  'CronList', 'RemoteTrigger', 'Monitor', 'TaskOutput', 'TaskStop', 'SendMessage', 'ListAgents',
  'BashOutput', 'KillShell'])
const SKILL_TOOLS = new Set(['Skill', 'ToolSearch', 'SlashCommand', 'ListMcpResourcesTool',
  'ReadMcpResourceTool'])

// This workspace's own defaults for the README's `memoryServers`,
// `memoryCommands` and `serverStations` options.
const MEMORY_SERVERS = new Set(['hindsight-main', 'hindsight', 'memory'])
const MEMORY_COMMANDS = ['hindsight', 'hindsight-remember', 'hindsight-snapshot', 'hindsight-sync',
  'memory-index', 'recall']
const SERVER_STATIONS: Partial<Record<string, Station>> = {
  codegraph: 'files', 'agent-watch': 'agents', playwright: 'browser',
}
const COMMAND_STATIONS: Partial<Record<string, Station>> = {
  herdr: 'agents', hc: 'agents', 'herdr-brief': 'agents', 'herdr-relay': 'agents', 'herdr-wait': 'agents',
}
// A path part that makes a file a memory: the data repository's `auto-memory`
// folder and Claude Code's own `<projects>/<slug>/memory/`. Names, not paths.
const MEMORY_PARTS = ['auto-memory']

const FILE_COMMANDS = new Set(['cat', 'head', 'tail', 'less', 'more', 'grep', 'egrep', 'fgrep', 'rg',
  'ag', 'find', 'ls', 'mv', 'cp', 'rm', 'mkdir', 'rmdir', 'touch', 'sed', 'tee', 'wc', 'sort', 'uniq',
  'cut', 'diff', 'stat', 'du', 'ln', 'chmod', 'chown', 'realpath', 'readlink', 'file', 'tree', 'zcat',
  'gzip', 'gunzip'])
const NETWORK_COMMANDS = new Set(['curl', 'wget', 'gh', 'ssh', 'scp', 'sftp', 'rsync', 'nc', 'ncat',
  'telnet', 'ftp', 'ping', 'dig', 'nslookup', 'whois', 'http', 'https'])
const NETWORK_SUBCOMMANDS: Record<string, Set<string>> = {
  git: new Set(['push', 'pull', 'fetch', 'clone', 'ls-remote']),
  npm: new Set(['install', 'i', 'ci', 'publish', 'update']),
  pnpm: new Set(['install', 'add']), yarn: new Set(['install', 'add']),
  pip: new Set(['install', 'download']), pip3: new Set(['install', 'download']),
  uv: new Set(['sync', 'add', 'pip']), docker: new Set(['pull', 'push', 'login']),
  apt: new Set(['install', 'update']), 'apt-get': new Set(['install', 'update']),
}
const NEUTRAL = new Set(['cd', 'pushd', 'popd', 'export', 'set', 'unset', 'echo', 'printf', 'true',
  'false', ':', 'test', '[', '[[', 'sleep', 'wait', 'source', '.', 'local', 'read', 'shift', 'exit',
  'return'])
const KEYWORDS = new Set(['for', 'do', 'done', 'if', 'then', 'else', 'elif', 'fi', 'while', 'until',
  'case', 'esac', 'in', '!', '{', '}', '(', ')', 'time', 'function', 'select'])
const WRAPPERS = new Set(['sudo', 'env', 'timeout', 'nohup', 'xargs', 'nice', 'ionice', 'stdbuf',
  'exec', 'command', 'builtin', 'watch', 'strace', 'chronic', 'unbuffer'])
// A wrapper's options that take the next word as their argument.
const WRAPPER_ARGS: Record<string, Set<string>> = {
  sudo: new Set(['-u', '-g', '-C', '-D', '-h', '-p', '-r', '-t', '-U']),
  timeout: new Set(['-s', '-k', '--signal', '--kill-after']),
  xargs: new Set(['-I', '-n', '-P', '-d', '-L', '-s', '-E', '-a']),
  nice: new Set(['-n']), ionice: new Set(['-c', '-n', '-p']),
  env: new Set(['-u', '-C', '-S']), watch: new Set(['-n']), strace: new Set(['-e', '-o', '-p']),
}
const SHELLS = new Set(['bash', 'sh', 'zsh', 'dash'])
const INTERPRETERS = new Set(['python', 'python3', 'python3.11', 'python3.12', 'node', 'ruby', 'perl',
  'deno', 'bun', 'tsx'])
// Options after which an interpreter runs code from the line, not a script.
const INLINE = new Set(['-c', '-e', '-p', '-r', '-E', '--eval', '--print'])

// An element of a word list, '' past its end (the index checks are the caller's).
const at = (list: readonly string[], i: number): string => list[i] ?? ''

const LINE_LIMIT = 8000 // the README reads a line up to its first 8,000 characters
const MAX_NESTING = 64 // nested $( levels scanned before a span runs to the end of the line
const ASSIGN = /^[A-Za-z_][A-Za-z0-9_]*\+?=/ // `x=1`, and `x+=1` appends
const NAME_START = /[A-Za-z_]/ // a shell name, ASCII
const NAME_CHARS = /[A-Za-z0-9_]/
const HEREDOC = /<<-?\s*(['"]?)([A-Za-z_][A-Za-z0-9_]*)\1/g
// C0, DEL, C1 (U+009B is a one-byte CSI) and the bidi overrides: none reaches the screen.
const CONTROL = /[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]+/g

export const clean = (text: unknown, limit = 60): string => {
  const s = String(text ?? '').replace(CONTROL, ' ').trim()
  return s.length <= limit ? s : `${s.slice(0, limit - 1)}…`
}

// ============================================================
// Shell lines
// ============================================================

export const stripHeredocs = (text: string): string => {
  const lines = text.split('\n')
  const out: string[] = []
  let i = 0
  while (i < lines.length) {
    const line = at(lines, i)
    out.push(line)
    const tags = [...line.matchAll(HEREDOC)].map(m => m[2] ?? '')
    i += 1
    for (const tag of tags) {
      while (i < lines.length && at(lines, i).trim() !== tag) i += 1
      i += 1
    }
  }
  return out.join('\n')
}

// The index of the `)` closing a `$(` whose body starts at j, or text.length.
// Quoted text and escaped characters inside the body are skipped, so the `)` in
// `$(echo ")")` does not close it. A nested `$(` is scanned by its own call, with
// fresh quotes, also inside double quotes, so `$(echo "$(date)")` closes where the
// shell closes it; a `$((` arithmetic span is skipped to its `))`, and a bare `(`
// (a subshell) is matched by its own `)`. Past MAX_NESTING levels the span runs
// to the end of the line instead of recursing further, so a crafted line cannot
// exhaust the stack.
const closingParen = (text: string, j: number, level = 0): number => {
  let quote = ''
  let parens = 0
  const n = text.length
  if (level > MAX_NESTING) return n
  while (j < n) {
    const ch = text.charAt(j)
    if (quote === "'") {
      if (ch === "'") quote = ''
    } else if (ch === '\\') {
      j += 1
    } else if (text.startsWith('$((', j)) {
      let depth = 2
      j += 3
      while (j < n && depth > 0) {
        const c = text.charAt(j)
        if (c === '(') depth += 1
        else if (c === ')') depth -= 1
        j += 1
      }
      continue
    } else if (text.startsWith('$(', j)) {
      j = closingParen(text, j + 2, level + 1)
    } else if (quote === '"') {
      if (ch === '"') quote = ''
    } else if (ch === "'" || ch === '"') {
      quote = ch
    } else if (ch === '(') {
      parens += 1
    } else if (ch === ')') {
      if (parens === 0) return j
      parens -= 1
    }
    j += 1
  }
  return n
}

// The line with every $(...) and backtick span blanked, and the inner lines.
const substitutions = (text: string): [string, string[]] => {
  const inner: string[] = []
  let out = ''
  let i = 0
  const n = text.length
  let single = false // the shell expands nothing in single quotes or after a backslash
  let double = false
  while (i < n) {
    const ch = text.charAt(i)
    if (single) {
      single = ch !== "'"
      out += ch
      i += 1
      continue
    }
    if (ch === '\\' && i + 1 < n) {
      out += text.slice(i, i + 2)
      i += 2
      continue
    }
    if (ch === "'" && !double) {
      single = true
      out += ch
      i += 1
      continue
    }
    if (ch === '"') {
      double = !double
      out += ch
      i += 1
      continue
    }
    if (text.startsWith('$(', i) && !text.startsWith('$((', i)) {
      const k = closingParen(text, i + 2)
      inner.push(text.slice(i + 2, k))
      out += ' '
      i = k + 1
    } else if (text.charAt(i) === '`') {
      let j = text.indexOf('`', i + 1)
      if (j === -1) j = n
      inner.push(text.slice(i + 1, j))
      out += ' '
      i = j + 1
    } else {
      out += text.charAt(i)
      i += 1
    }
  }
  return [out, inner]
}

// The simple commands of a line: split on ; & | and newlines outside quotes. A `(`
// where a command would start opens a subshell, so it and its `)` split too:
// `(cd x; curl y)` is `cd x` and `curl y`.
export const splitCommands = (text: string): string[] => {
  const parts: string[] = []
  let buf = ''
  let quote = ''
  let i = 0
  let sub = 0
  let blank = true // buf holds only spaces and tabs, the shell's blanks
  while (i < text.length) {
    const ch = text.charAt(i)
    if (quote) {
      if (ch === '\\' && quote === '"' && i + 1 < text.length) { buf += text.slice(i, i + 2); i += 2; continue }
      if (ch === quote) quote = ''
      buf += ch
    } else if (ch === '\\' && i + 1 < text.length) {
      buf += text.slice(i, i + 2); blank = false; i += 2; continue
    } else if (ch === "'" || ch === '"') {
      quote = ch; buf += ch; blank = false
    } else if (';&|\n'.includes(ch) || (ch === '(' && blank) || (ch === ')' && sub > 0)) {
      if (ch === '(') sub += 1
      else if (ch === ')') sub -= 1
      parts.push(buf); buf = ''; blank = true
    } else {
      buf += ch
      blank = blank && (ch === ' ' || ch === '\t')
    }
    i += 1
  }
  parts.push(buf)
  return parts.filter(p => p.trim())
}

// Shell words: quotes, backslashes and a `#` comment at a word's start. The same
// rules, line for line, as `_tokens` in scripts/lot-map.py; words split only on
// space, tab, CR and newline, as the shell does. An array assignment is one word:
// an unquoted `(` right after an unquoted `name=` opens it and blanks inside it do
// not split, up to its unquoted `)`. Deciding here, where quotes are still visible,
// keeps `x='(a' curl y` two words apart from the array `x=(a b)`.
const words = (segment: string): string[] => {
  const out: string[] = []
  let cur = ''
  let has = false
  let quote = ''
  // how far the word so far is an unquoted `name=`, one character at a time:
  // 0 empty, 1 a name, 2 a name and `=` or `+=`, 3 anything else, 4 a name and `+`
  let shape = 0
  let array = false // inside an unquoted `name=(...)`
  for (let i = 0; i < segment.length; i += 1) {
    const ch = segment.charAt(i)
    if (quote) {
      if (ch === quote) quote = ''
      else if (ch === '\\' && quote === '"' && i + 1 < segment.length) {
        // in double quotes a backslash escapes only $ ` " \ and newline
        const next = segment.charAt(i + 1)
        cur += '$`"\\\n'.includes(next) ? next : ch + next
        i += 1
      }
      else cur += ch
      continue
    }
    if (ch === "'" || ch === '"') { quote = ch; has = true; shape = 3; continue }
    if (ch === '\\' && i + 1 < segment.length) { cur += segment.charAt(i + 1); i += 1; has = true; shape = 3; continue }
    if (ch === '#' && !has && !cur) break
    if (ch === '(' && !array && shape === 2) { cur += ch; array = true; shape = 3; continue }
    if (ch === ')' && array) { cur += ch; array = false; continue }
    if (' \t\r\n'.includes(ch) && array) { cur += ' '; continue }
    if (' \t\r\n'.includes(ch)) { if (has || cur) out.push(cur); cur = ''; has = false; shape = 0; continue }
    cur += ch; has = true
    if (shape === 0) shape = NAME_START.test(ch) ? 1 : 3
    else if (shape === 1) shape = NAME_CHARS.test(ch) ? 1 : ch === '=' ? 2 : ch === '+' ? 4 : 3
    else if (shape === 4) shape = ch === '=' ? 2 : 3 // a name and `+`: `x+=(a)` appends to an array
    else shape = 3
  }
  if (has || cur) out.push(cur)
  return out
}

const base = (word: string): string => {
  const name = (word.replace(/\\/g, '/').split('/').pop() ?? word).toLowerCase()
  return name.endsWith('.exe') ? name.slice(0, -4) : name
}

const stem = (word: string): string => {
  const name = base(word)
  for (const ext of ['.py', '.sh', '.js', '.mjs', '.ts']) if (name.endsWith(ext)) return name.slice(0, -ext.length)
  return name
}

// The README's match: the command itself, or it followed by `-`, `_` or `.`.
const named = (name: string, names: Iterable<string>): string | undefined => {
  for (const c of names) {
    if (name === c || (name.startsWith(c) && '-_.'.includes(name.charAt(c.length)))) return c
  }
  return undefined
}

type Part = [Station | 'neutral', string]

const simple = (ws: string[], depth: number): Part[] => {
  if (ws.length && ['for', 'select', 'case'].includes(at(ws, 0))) return []
  let i = 0
  while (i < ws.length) {
    const w = at(ws, i)
    if (KEYWORDS.has(w) || ASSIGN.test(w)) { i += 1; continue } // an array `x=(a b)` is one word already, see `words`
    const b = base(w)
    if ((b === 'uv' || b === 'uvx') && ws[i + 1] === 'run') {
      i += 2 // `uv run x` runs x; a loop, so a long chain needs no recursion
      continue
    }
    if (WRAPPERS.has(b)) {
      i += 1
      while (i < ws.length && (at(ws, i).startsWith('-') || ASSIGN.test(at(ws, i)) || (b === 'timeout' && /^\d/.test(at(ws, i))))) {
        i += WRAPPER_ARGS[b]?.has(at(ws, i)) ? 2 : 1
      }
      continue
    }
    break
  }
  if (i >= ws.length) return []
  let name = base(at(ws, i))
  if (name.startsWith('$')) name = 'sh' // a command held in a variable: its name is not known here
  const rest = ws.slice(i + 1)
  if (SHELLS.has(name) || name === 'eval') {
    if (name === 'eval') return classifyLine(rest.join(' '), depth + 1)
    const k = rest.indexOf('-c')
    if (k !== -1 && k + 1 < rest.length) return classifyLine(at(rest, k + 1), depth + 1)
    const script = rest.find(r => !r.startsWith('-'))
    if (script) name = stem(script)
  } else if (INTERPRETERS.has(name) || name.startsWith('python3')) {
    const script = rest.find(r => !r.startsWith('-'))
    const m = rest.indexOf('-m')
    if (script && !rest.some(r => INLINE.has(r)) && m === -1) name = stem(script)
    else if (m !== -1 && m + 1 < rest.length) name = at(rest, m + 1).toLowerCase()
  } else if (name === 'npx') {
    return [[rest.includes('-y') || rest.includes('--yes') ? 'network' : 'shell', 'npx']]
  }
  if (name === 'find') {
    const k = rest.indexOf('-exec') !== -1 ? rest.indexOf('-exec') : rest.indexOf('-execdir')
    if (k !== -1) return [['files', 'find'], ...simple(rest.slice(k + 1).filter(r => ![';', '+', '{}'].includes(r)), depth)]
    return [['files', 'find']]
  }
  if (named(name, MEMORY_COMMANDS)) return [['memory', name]]
  const configured = named(name, Object.keys(COMMAND_STATIONS))
  const configuredStation = configured ? COMMAND_STATIONS[configured] : undefined
  if (configuredStation) return [[configuredStation, name]]
  if (NEUTRAL.has(name)) return [['neutral', name]]
  if (NETWORK_COMMANDS.has(name)) return [['network', name]]
  const sub = NETWORK_SUBCOMMANDS[name]
  if (sub && rest.slice(0, 2).some(r => sub.has(r))) return [['network', name]]
  if (FILE_COMMANDS.has(name)) return [['files', name]]
  return [['shell', name]]
}

export const classifyLine = (line: string, depth = 0): Part[] => {
  if (depth > 4) return [['shell', 'sh']]
  const [text, inner] = substitutions(stripHeredocs(line.slice(0, LINE_LIMIT)))
  const found: Part[] = []
  for (const segment of splitCommands(text)) {
    const ws = words(segment).filter(w => !/^\d*[<>]/.test(w))
    if (ws.length) found.push(...simple(ws, depth))
  }
  for (const s of inner) found.push(...classifyLine(s, depth + 1))
  return found
}

// One station for a whole line: Network wins; then a configured station; then
// Memory; Files only when every command works on files and nothing else; else Shell.
export const shellStation = (command: string): [Station, string] => {
  const all = classifyLine(command)
  const parts = all.filter(p => p[0] !== 'neutral') as [Station, string][]
  if (parts.length === 0) return ['shell', all[0]?.[1] ?? 'sh']
  const net = parts.find(p => p[0] === 'network')
  if (net) return net
  const other = parts.find(p => !['files', 'shell', 'memory'].includes(p[0]))
  if (other) return other
  const mem = parts.find(p => p[0] === 'memory')
  if (mem) return mem
  const first = parts[0]?.[1] ?? 'sh'
  if (parts.every(p => p[0] === 'files')) return ['files', first]
  return ['shell', parts.find(p => p[0] === 'shell')?.[1] ?? first]
}

// ============================================================
// Tool calls
// ============================================================

const twoParts = (path: string): string => path.replace(/\\/g, '/').split('/').filter(Boolean).slice(-2).join('/')

export const host = (url: string): string => {
  const m = /^([a-zA-Z][a-zA-Z0-9+.-]*):\/\/([^/?#]*)/.exec(url || '')
  if (!m) return '?'
  const scheme = m[1] ?? '?'
  const h = (m[2] ?? '').split('@').pop() ?? ''
  if (!h || h.includes('\\') || h.includes(' ')) return scheme
  return (h.match(/:/g) ?? []).length === 1 ? (h.split(':')[0] ?? h) : h
}

const inMemory = (path: string): boolean => {
  if (!path) return false
  const parts = path.replace(/\\/g, '/').split('/').filter(Boolean)
  if (parts.some(p => MEMORY_PARTS.includes(p))) return true
  const at = parts.indexOf('projects')
  return at !== -1 && parts[at + 2] === 'memory'
}

const str = (v: unknown): string => (typeof v === 'string' ? v : '')

// `(station, label)` for one tool call; `input` is the call's own arguments.
export const toolStation = (tool: string, input: Record<string, unknown>): [Station, string] => {
  if (tool.startsWith('mcp__')) {
    const [, server = '', name = ''] = tool.split('__')
    if (MEMORY_SERVERS.has(server)) {
      const id = str(input.id) || str(input.document_id) || str(input.memory_id)
      return ['memory', `${server}: ${name}${id ? ` ${clean(id, 24)}` : ''}`]
    }
    const configured = SERVER_STATIONS[server]
    if (configured) return [configured, `${server}: ${name}`]
    if (server.includes('browser') || server.includes('chrome')) return ['browser', `${server}: ${name}`]
    return ['network', `${server}: ${name}`]
  }
  if (FILE_TOOLS.has(tool)) {
    if (tool === 'Grep' || tool === 'Glob') return [inMemory(str(input.path)) ? 'memory' : 'files', tool]
    const path = str(input.file_path) || str(input.notebook_path)
    return [inMemory(path) ? 'memory' : 'files', path ? `${tool} ${twoParts(path)}` : tool]
  }
  if (SHELL_TOOLS.has(tool)) {
    const [station, cmd] = shellStation(str(input.command))
    const desc = str(input.description)
    return [station, `${desc ? clean(desc) : cmd}${input.run_in_background ? ' (bg)' : ''}`]
  }
  if (tool === 'WebSearch') return ['network', 'web search']
  if (tool === 'WebFetch') return ['network', `WebFetch ${host(str(input.url))}`]
  if (YOU_TOOLS.has(tool)) return ['you', tool]
  if (AGENT_TOOLS.has(tool)) {
    const desc = str(input.description) || str(input.subagent_type)
    return ['agents', `${tool} ${clean(desc, 40)}`.trim()]
  }
  if (tool === 'ToolSearch') return ['skills', 'loading tools']
  if (SKILL_TOOLS.has(tool)) return ['skills', `${tool} ${clean(str(input.skill) || str(input.command), 30)}`.trim()]
  if (/^(browser|Browser|Chrome)/.test(tool)) return ['browser', tool]
  return ['shell', tool]
}

export const shortModel = (model: string): string => {
  const m = /^claude-([a-z]+)-(\d+)-(\d+)/.exec(model || '')
  return m ? `${m[1]} ${m[2]}.${m[3]}` : clean(model || '?', 40)
}

// A settings hook's added context that is memory: Hindsight's injections.
export const isMemoryText = (text: string): boolean => /hindsight|memory_facts|<memory/i.test(text)
