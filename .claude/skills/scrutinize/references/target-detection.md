# Target Detection Protocol - /scrutinize

**Consumed by:** `.claude/skills/scrutinize/SKILL.md`
**Last Updated:** 2026-10-31

Hybrid auto-detect with user confirmation. The skill reads recent conversation and git state, picks the most likely target, announces it in one line, and waits for confirmation or redirect.

## Detection Priority (first match wins)

| # | Condition | Target |
|---|---|---|
| 1 | Explicit argument (`execution` / `workspace` / `file:<path>` / `dir:<path>` / `trajectory:<run_id>`) | Use argument; skip confirmation |
| 1b | A `/implement` run completed in the current session AND `_trajectory_*.jsonl` exists under `runtime:implement/logs` from this session | `trajectory:<latest run_id>` (single-turn detection only — does not persist across sessions) |
| 2 | `git status` shows uncommitted changes AND edits occurred this session | `execution` (scope from git) |
| 3 | None of the above | Ask (see Menu) |

The `plan` target is retired. A typed `/scrutinize plan` prints exactly `The plan target is retired. Run /scrutinize file:<path of the plan>.` and stops. A plan is reviewed as `file:<path>`.


## Confirmation Line Format

After auto-detection at priorities 1b-2, print exactly one line:

```
Target detected: <label>. Proceeding with VIIA pass. Redirect with: execution | file:<path> | dir:<path> | workspace.
```

Wait for user response. Any redirect supersedes auto-detection. A silence followed by a correction ("wait, I meant workspace") also supersedes.

## Menu (priority 3)

If nothing obvious:

```
No obvious target in context. What should I scrutinize?

1. execution
2. file:<path>
3. dir:<path>
4. workspace
5. trajectory:<run_id>

Reply with the number, the name, or an explicit argument like `file:path/to/thing.md`.
```

Wait for response.

## Scope Resolution per Target

### execution (hybrid)

Resolve scope in this order:

1. If `git status --porcelain` shows changes OR `git log --since="2 hours ago" --oneline` returns commits, use the union of both sets of files.
2. Else prompt:

```
Scope for execution review:
1. Uncommitted changes only (git status)
2. Last commit
3. Last N commits (specify N)
4. Specific SHA range (specify)

Default: 1
```

### file:<path>
Source: read the file at `<path>`. If path does not exist, stop and report the missing path.
It is a **plan file** when its path resolves under registry name `plans` (`.venv/bin/python scripts/data-path.py path plans`) or its first line starts with `# Plan:`. A plan file gets refutation and the goldfish `plan` lens, no external code voice, no Phase 4.5 promotion, and refuses `--relentless`.

### dir:<path>
Source: glob `<path>/**/*` with these exclusions: `.git/`, `node_modules/`, `outputs`, `.sessions/`, `*.pen`, binary files. Review each file per its type.

### workspace
Source: the entire main workspace. Dispatch 5 parallel specialist agents per `workspace-areas.md`.

### trajectory:<run_id>

Source: read `_trajectory_{run_id}.jsonl` under the directory `.venv/bin/python scripts/data-path.py path runtime:implement/logs` prints, parse all events, group by phase and step number, build a normalised view (per-step record: start, end, files, validation, deviation).

**Resolution with explicit error paths:**

1. **Exact match first**: try to read `_trajectory_{run_id}.jsonl` in that directory literally.
2. **If exact match fails**, glob `_trajectory_*{run_id}*.jsonl` in that directory (substring match):
   - **Zero matches** → error: `"No trajectory matches '{run_id}'. Available (5 most recent): <list>."` and halt.
   - **One match** → use it; print `"Resolved '{input}' -> '{full_run_id}' (substring match)."` and proceed.
   - **Multiple matches** → menu showing 5 most-recent matches, ask CEO to disambiguate.
3. **Malformed run_id**: if input does not match regex `^\d{4}-\d{2}-\d{2}_\d{6}_[a-z0-9-]+$` AND no substring matches found, error: `"Malformed run_id '{input}'. Expected format: YYYY-MM-DD_HHMMSS_<plan-slug> (e.g. 2026-05-27_134522_r12-trajectory-evaluation). Run /implement first if no trajectories exist yet."` and halt.

Run-id slug equals the part after the final `_` in the run_id (matches `scripts/implement-trajectory-log.py` minting rule).

## Slug and run id

Phase 0 step 5 and the Phase 5 report name use one slug:

- `execution` and `workspace`: that word.
- `trajectory:<run_id>`: `trajectory-` and the part after the final `_`.
- every other target: the last path segment, with a leading `\d{4}-\d{2}-\d{2}-` and the extension removed, lowercased, every run of characters outside `[a-z0-9-]` replaced by `-`, cut to 40 characters.

The slug carries no date, so the report name `<YYYY-MM-DD>-<slug>.md` holds exactly one. The run id is `<YYYY-MM-DD>_<HHMMSS>_<slug>`, so two runs never share one, and neither do their scratch directories (`<scratch>/scrutinize/<run_id>/`).

## Worked Examples

**Example 2 - explicit argument bypasses confirmation:**

```
User: /scrutinize workspace
Skill: no confirmation needed; immediately dispatches the 5 specialist agents.
```

**Example 3 - nothing obvious:**

```
User: /scrutinize
[Clean git.]
Skill: prints the 5-option menu and waits.
```

**Example 4 - trajectory auto-detection right after /implement:**

```
User: /implement <plans>/<plan>.md
[/implement runs to completion, writes _trajectory_<run_id>.jsonl under runtime:implement/logs]
User: /scrutinize
Skill: "Target detected: trajectory:<run_id>. Proceeding with VIIA pass. Redirect with: execution | file:<path> | dir:<path> | workspace | trajectory:<run_id>."
User: ok
Skill: proceeds with trajectory-target VIIA pass per references/trajectory-evaluation.md.
```

**Example 5 - partial-match resolution:**

```
User: /scrutinize trajectory:r12-trajectory-evaluation
Skill: exact match fails (missing date prefix). Globs _trajectory_*r12-trajectory-evaluation*.jsonl.
       One match found: 2026-05-27_134522_r12-trajectory-evaluation.
       Prints: "Resolved 'r12-trajectory-evaluation' -> '2026-05-27_134522_r12-trajectory-evaluation' (substring match)."
       Proceeds.
```
