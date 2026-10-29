---
name: sync
disable-model-invocation: true
description: "Manually trigger workspace sync - git pull of the code/data clones, then a push-all backup. EXPLICIT INVOCATION ONLY - never auto-trigger."
argument-hint: "[pull|backup]"
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "2.0"
allowed-tools: "Bash(.venv/bin/python:*), Bash(git:*), Read"
model: haiku
x-heading-capability:
  what: >
    Manually syncs the workspace with plain git: pulls the latest code (and data)
    with `git pull --ff-only`, then backs up via `scripts/push-all.py`.
  how: >
    Run /sync (explicit-invocation-only, never auto-triggers) to pull then back up,
    or scope it with /sync pull | backup. pull = git pull --ff-only on the engine
    and data clones; backup = push-all.py.
  when: >
    Use to pull updates or back up between sessions. For a backup alone use
    /backup (push-all).
x-heading-routing:
  category: Operations
  triggers:
    - NEVER auto-trigger. Explicit `/sync` only.
  exclusions:
    - Backup alone -> /backup
  router: manual
---
# Workspace Sync

Manually sync the workspace with two non-destructive git steps (this replaced
the retired `workspace-sync.py` on 2026-06-26).

## What Gets Synced

1. **Pull (code + data)** - `git pull --ff-only` on the engine clone (`.heading-os`)
   and the data overlay (`.heading-os-data`). Fast-forward only, so a divergent
   local history surfaces as a plain error instead of an implicit merge.
2. **Backup (push)** - `.venv/bin/python scripts/push-all.py` commits and pushes BOTH repos
   to their private `origin/main`. That push runs the pre-push secret scan and
   the `[0 0]` ahead/behind verification.

> First-run record recovery after a clean deploy is a separate one-shot:
> `.venv/bin/python scripts/import-legacy-records.py --from <old-records-path>` (local,
> non-destructive). It is NOT part of /sync.

## Options

- `/sync` - pull, then backup
- `/sync pull` - `git pull --ff-only` on the engine and data clones only
- `/sync backup` - `.venv/bin/python scripts/push-all.py` only

## Steps

1. Resolve the engine root (the workspace you launched from) and the data root
   (the `.heading-os-data` sibling, or `HEADING_OS_DATA` if set).
2. For **pull**: run `git -C <engine-root> pull --ff-only` and
   `git -C <data-root> pull --ff-only`. Show each result.
3. For **backup**: run `.venv/bin/python scripts/push-all.py` and show its output.
4. For bare `/sync`: run the pull sequence first.
5. Then run the backup.
6. If a step fails, explain plainly and suggest a fix:
   - `git pull` non-fast-forward: "Local history diverged - reconcile manually
     (`git -C <root> status`) before syncing; /sync will not force or merge."
   - push-all secret-scan refusal: "A tracked file looks like it holds a secret -
     move it to `.env` and retry; never pass `--no-verify`."
   - push-all exit `3`: NOT a failure. At least one repository was skipped (a branch
     that is not `main`, an unarmed engine test gate). Every skipped repo is still
     committed locally. Read its headline: `Partial: N of M` means the rest pushed and
     verified; `NOTHING PUSHED: all M` means no new off-machine copy exists. Report
     the shape, name the repo, quote the reason. Full guidance: `/backup` SKILL.md.
   - Git auth: "GH_TOKEN missing or expired - check the engine `.env`."

## Never

- Never delete files as part of sync. There is no orphan-deletion step anymore.
- Never pass `--no-verify` or force-push to work around a failed gate.
