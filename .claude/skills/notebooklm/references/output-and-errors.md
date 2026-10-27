# NotebookLM - Output Routing, Errors, and Bridges

**Data addresses.** Resources in the data repository are named here by their
logical name, never by a path. Resolve the name before the file operation and
use what it prints:

    .venv/bin/python scripts/data-path.py path <name>

Use `read <name>` for a source that may legitimately be absent, and
`read --required <name>` for one whose absence is a defect. An unknown name
refuses; it never resolves to a plausible directory, and a path written out
here goes stale the day the directory moves.

Consumed by: `.claude/skills/notebooklm/SKILL.md` Phase 2 (output) and Phase 3 (error handling). Static catalogues - the orchestration lives in SKILL.md.

---

## Output Routing

All outputs go to `notebooklm-output` with type-based subdirectories:

| Type | Directory | Extensions |
|------|-----------|-----------|
| Audio overviews | `audio/` | .m4a |
| Briefing reports | `reports/` | .md |
| Research results | `research/` | .md (saved manually) |
| Saved queries | `queries/` | .md (saved on request) |
| Other artifacts | `downloads/` | .mp4, .png, .pdf, .pptx, .json, .md, .html, .csv |

Filename pattern: `YYYY-MM-DD-<slug>.<ext>` where `<slug>` is kebab-case, max 40 characters.

---

## Odin/ZK Bridge

This skill does NOT directly invoke `/odin` or `/zk`. It suggests handoffs at natural points:

- After **query**: "Get Odin's take? (`/odin consult [topic]`)" or "Save as ZK note? (`/zk add`)"
- After **research discovery**: "Feed sources to Odin? (`/odin learn [url]`)"
- After **report**: "Capture this? (`/odin log` for CEO; `/zk distill` to the knowledge base)"

The CEO approves, and Claude invokes the downstream skill as a separate operation.

---

## Error Handling

| Error | Detection | Recovery Message |
|---|---|---|
| Auth expired | `nlm login --check` exit != 0 | "Auth expired. Run `nlm login` in your terminal." |
| CLI not found | "command not found" or "No such file" in Bash output | "nlm not installed. Run: `pip install notebooklm-mcp-cli`" |
| Rate limited | "rate" in error message or HTTP 429 | "Rate limit reached. Wait ~1 hour or upgrade to NotebookLM Pro." |
| API changed | Unexpected JSON structure or HTTP error | "NotebookLM API may have changed. Try: `pip install --upgrade notebooklm-mcp-cli`" |
| Generation timeout | Polling exceeded max iterations | "Timed out. Check notebooklm.google.com directly." |
| Network error | Connection refused or timeout | "Network error reaching NotebookLM. Check internet." |
| Notebook not found | "not found" in error response | "Notebook ID not recognized. Run `/notebooklm status` to see available notebooks." |

Always provide specific, actionable recovery. Never show raw stack traces.
