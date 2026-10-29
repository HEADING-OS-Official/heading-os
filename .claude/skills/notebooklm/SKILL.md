---
name: notebooklm
description: >
  Google NotebookLM CLI integration. Creates topic notebooks, adds sources
  (URLs, text, files), queries with grounded citations, generates audio
  overviews (AI podcast), runs research discovery, generates briefing
  reports, downloads artifacts.
  Uses undocumented Google APIs via notebooklm-mcp-cli CLI tool.
  Triggers on "notebooklm", "audio overview", "podcast from sources",
  "create a notebook", "notebook research". Does NOT trigger on generic
  "research" (use /osint), "notes" (use /zk), or "podcast" without
  clear NotebookLM context (could be /yt-pulse).
argument-hint: "[status|create|add|query|audio|research|report|describe|download] [args]"
allowed-tools: "Read, Write, Bash(nlm:*), Bash(.venv/bin/python:*), Glob"
model: haiku
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "1.0"
x-heading-capability:
  what: >
    Google NotebookLM control from the workspace - create topic notebooks, add sources (URLs, text, files), query with grounded citations, generate audio overviews (AI podcast), run research discovery, and produce briefing reports.
  how: >
    Run /notebooklm [status|create|add|query|audio|research|report|describe|download] [args]. Outputs land under notebooklm-output; requires nlm login first.
  when: >
    Use for NotebookLM-specific notebook work and AI audio overviews. For general target research use /osint; for atomic knowledge notes use /zk; for topic-based YouTube podcasts use /yt-pulse.
x-heading-routing:
  category: Intel
  triggers:
    - notebooklm
    - audio overview
    - podcast from sources
    - create a notebook
    - notebook research
    - add sources to notebook
  exclusions:
    - '"research [company]" -> /osint'
    - '"add a note" -> /zk'
    - '"podcast" alone -> /yt-pulse'
    - '"presentation" -> /pptx-generator'
  router: auto
---
# NotebookLM Integration

**Data addresses.** Resources in the data repository are named here by their
logical name, never by a path. Resolve the name before the file operation and
use what it prints:

    .venv/bin/python scripts/data-path.py path <name>

Use `read <name>` for a source that may legitimately be absent, and
`read --required <name>` for one whose absence is a defect. An unknown name
refuses; it never resolves to a plausible directory, and a path written out
here goes stale the day the directory moves.

CLI wrapper for Google NotebookLM via `notebooklm-mcp-cli`. Creates topic notebooks, ingests sources, queries with grounded citations, generates audio overviews, runs research discovery, and bridges to Odin for knowledge ingestion.

Uses undocumented Google APIs - may break without notice.

---

## CLI Access

Use this exact invocation pattern for ALL commands:

```
NLM="$(command -v nlm)"
NO_COLOR=1 PYTHONIOENCODING=utf-8 "$NLM" <subcommand> [flags]
```

Always set `NLM` as a variable at the start of each Bash call, then use `"$NLM"` for the command. The `NO_COLOR=1 PYTHONIOENCODING=utf-8` prefix forces plain UTF-8 output.

---

## Variables

- `$ARGUMENTS` - Mode and parameters. Format: `[mode] [target/args]`
- Modes: `status`, `create`, `add`, `query`, `audio`, `research`, `report`, `describe`, `download`

## Phase 0: Auth Validation

Run before EVERY mode. No exceptions.

1. Run (Bash, timeout 15000):
   ```bash
   NLM="$(command -v nlm)"
   NO_COLOR=1 PYTHONIOENCODING=utf-8 "$NLM" login --check
   ```
2. If exit code 0: proceed to requested mode
3. If exit code != 0: STOP. Display:

```
NotebookLM authentication expired. Run this in your terminal:

  nlm login

This opens a browser window for Google sign-in. After login completes, retry your command.
```

Do NOT proceed with any mode. Do NOT attempt to auto-login (requires browser interaction).

## Phase 1: Mode Dispatch

Parse mode from `$ARGUMENTS` or natural language:

- **No arguments / empty / just "/notebooklm"** -> show mode menu
- "status" / "check" / "list notebooks" -> `status`
- "create" / "new notebook" -> `create`
- "add" / "sources" / URLs detected in args -> `add`
- "query" / "ask" / question mark in args -> `query`
- "audio" / "podcast" / "overview" -> `audio`
- "research" / "discover" / "find sources" -> `research`
- "report" / "briefing" / "study guide" -> `report`
- "describe" / "summarize notebook" -> `describe`
- "download" / "save artifact" / "get artifact" -> `download`

### Mode Menu (when no arguments given)

Display this and wait for selection:

```
## NotebookLM

| # | Mode | What it does |
|---|------|-------------|
| 1 | **status** | Auth check + list all notebooks |
| 2 | **create** | Create a new notebook |
| 3 | **add** | Add sources (URLs, text, files) to a notebook |
| 4 | **query** | Query a notebook with grounded citations |
| 5 | **audio** | Generate audio overview (AI podcast) |
| 6 | **research** | Run web discovery to find new sources on a topic |
| 7 | **report** | Generate a briefing doc from notebook sources |
| 8 | **describe** | Get AI summary of a notebook |
| 9 | **download** | Download any artifact from a notebook |

Usage: /notebooklm [mode] [args]
Examples:
- /notebooklm create "European Cybersecurity Market"
- /notebooklm add <notebook-id> https://example.com/article1 https://example.com/article2
- /notebooklm query <notebook-id> What are the key regulatory trends?
- /notebooklm audio <notebook-id>
- /notebooklm research "AI governance"
```

---

## Phase 2: Mode Execution

Per-mode flow lives in `references/mode-catalog.md`. Read ONLY the section for the dispatched mode - do not load all nine. The catalogue carries the CLI command, polling rules, JSON parsing notes, output template, and handoff offers for each mode.

Modes covered: `status`, `create`, `add`, `query`, `audio`, `research`, `report`, `describe`, `download`.

Each mode reuses the `NLM` variable + `NO_COLOR=1 PYTHONIOENCODING=utf-8` prefix from the CLI Access section above. When the catalog shows a bare `"$NLM" ...` invocation, SKILL.md callers must still set the variable at the start of every Bash call.

Polling pattern (audio, report): 15-second interval, max 20 iterations (5 minutes), check `status` field for `in_progress` / `completed` / `failed`. Research mode uses its own intervals (15s fast / 30s deep) per the catalog.

---

## Phase 3: Output, Bridges, and Error Handling

Output routing (type -> subdirectory mapping), Odin/ZK bridge handoff points, and the full error -> recovery message table live in `references/output-and-errors.md`. Read on demand when an error fires or a download is queued.

Filename pattern across all modes: `YYYY-MM-DD-<slug>.<ext>` where `<slug>` is kebab-case, max 40 characters. All outputs land under `notebooklm-output`.

---

## NEVER

1. **Never auto-login.** `nlm login` requires browser interaction. Always direct the user to run it manually.
2. **Never bypass Phase 0.** Auth check runs before every mode. No exceptions.
3. **Never write to shared state.** All outputs go to `notebooklm-output` only. Never write to the CRM, the five context homes, `knowledge/`, or any other shared path.
4. **Never create dependencies.** No other skill may require NotebookLM to function. This is additive.
5. **Never auto-upgrade the CLI.** Upgrades are manual and tested.
6. **Never invoke Odin/ZK directly.** Suggest handoffs. Let the CEO approve. Claude routes to the downstream skill.
7. **Never use the MCP server.** This skill uses CLI only. Do not add `notebooklm-mcp` to Claude Code settings or MCP config.

## Voice Rules

- Single hyphens `-` in prose, never double dashes
- Product names exactly as the company writes them
- The company's own terms per `.claude/rules/terminology.md`
- Language matches the user's language (Russian question = Russian output)
