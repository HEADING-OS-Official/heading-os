---
name: backup
disable-model-invocation: true
description: Backup the entire workspace to GitHub. EXPLICIT INVOCATION ONLY - never auto-trigger from natural language.
argument-hint: "(no arguments)"
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "1.4"
allowed-tools: "Bash(.venv/bin/python scripts/push-all.py:*)"
model: haiku
x-heading-capability:
  what: >
    Commits the engine and the data repository and pushes both to their private
    GitHub origin/main through `scripts/push-all.py`, so the workspace is backed
    up off-machine.
  how: >
    Explicit invocation only - type /backup (no arguments); never auto-triggers.
    It runs `scripts/push-all.py` and reports what each repository pushed.
  when: >
    Use to save the whole workspace to GitHub.
x-heading-routing:
  category: Operations
  triggers:
    - NEVER auto-trigger. Explicit `/backup` only.
  exclusions:
    - N/A
  compound: 'No'
  router: manual
---
Backup the entire workspace to GitHub.

## CEO two-part topology (engine + data): use `push-all.py`

The CEO workspace is split into two repos, each with its own private
`origin/main`. The ENGINE clone (`.heading-os`) holds code only. The DATA
overlay (`.heading-os-data`) holds all data and every artifact. One command
commits and pushes BOTH. It runs a pre-push secret scan and an ahead/behind
`[0 0]` verification, because a bare push can silently leave a ref behind:

```
.venv/bin/python scripts/push-all.py            # commit working-tree changes + push both
.venv/bin/python scripts/push-all.py -m "msg"   # custom commit message
.venv/bin/python scripts/push-all.py --no-commit  # push existing commits only
.venv/bin/python scripts/push-all.py --dry-run    # show what would happen, change nothing
```

It reads `GH_TOKEN` from the engine `.env` and refuses to push any tracked
secret-like file (`.env`, `.session`, `cookies.json`, `.sessions/`). It never
pushes the rebuildable index, which sits in the gitignored
runtime address `runtime:memory-index/cache`. The DATA overlay is pushed FIRST. The engine's pre-push hook runs the full suite inside the push, and data
is the only half that cannot be reconstructed.

**The operator typing `/backup` is the authorisation for this commit and push,
and nothing else is.** The skill carries `disable-model-invocation: true`, so the
model cannot reach it. Never commit or push on a message that did not invoke it.

**Exit `3` means at least one repository was skipped. Read the HEADLINE, not the
code, because it has two shapes and they mean opposite things to the operator.**

- **`Partial: N of M repo(s) not pushed.`** — the backup was partial, not failed.
  Report it that way. The repositories named below the headline were not pushed, for
  the reason printed next to each; everything else was pushed and verified.
- **`NOTHING PUSHED: all M repo(s) skipped.`** — nothing reached a remote and there
  is NO new off-machine copy. Never report this as a partial success. The
  pre-cutover single-repo mode pushes one repository, so exit `3` there is always
  this shape.

In both shapes every skipped repository is still committed locally, so nothing is
lost. Tell the operator which repository was skipped, quote the reason, and give the
remedy. The remedy is usually to merge the branch into `main` and run the command
again. For an unarmed engine test gate, it is
`.venv/bin/python scripts/install-git-hooks.py`. Do NOT re-run the command hoping for a
different answer.

Exit `1` and `2` are real failures that stopped the run. The causes are a security
refusal, a remote a repository must not push to, or an absent `GH_TOKEN`. They are
also a misconfigured data root, or a push that ran and did not verify.

Exit `2` with "REFUSING TO PUSH" naming a remote is the newest of those and is
different in kind from the others. It means a repository is aimed at a remote it
must not push to: the engine's own remote, or a repository GitHub reports as
public. Nothing was pushed and nothing was lost. The whole run stops rather than
continuing with the other repositories, because a wrong remote makes all of them
suspect. Report the remote it names to the operator and do not retry until it is
corrected.

Because the DATA overlay is pushed first, a failure at the ENGINE can still leave
DATA pushed and verified. Read the per-repository lines above the failure before
you tell the operator nothing was backed up. Exit `0` means everything went.

## If `push-all.py` cannot run

Stop. Report the error to the operator, quote it, and do nothing else. There is
no manual fallback. Every push that must complete goes through
`supervised_push` in `scripts/utils/git_push.py`, and `push-all.py` is the
caller that uses it. A hand-rolled `git add -A`, commit, and push skips the
secret scan and the `[0 0]` verification.

Important:
- Commit messages carry no `Co-Authored-By` trailer, in any form. Strip one from a prepared message before committing.
- Never pass `--no-verify`.
