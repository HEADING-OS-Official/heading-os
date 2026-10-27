---
name: linkedin-archive
description: Move the latest published LinkedIn content (article/post/comment) plus any attached images from the LinkedIn staging area (linkedin-drafts) into the typed datastore archive. Auto-fires on English or Russian "I published it on LinkedIn" phrasing. Asks for the content type if it cannot be inferred. Confirms before mutating disk.
argument-hint: "[slug]"
allowed-tools: "Read, Bash(.venv/bin/python:*), AskUserQuestion"
model: haiku
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "1.1"
x-heading-capability:
  what: >
    Moves a just-published LinkedIn piece (article, post, or comment) plus any attached images out of staging and into the typed datastore archive, linkedin-archive.
  how: >
    Auto-fires on "I published it on LinkedIn" (English or Russian), or run /linkedin-archive. Resolves type and images, shows a dry-run plan, and waits for approval before moving via scripts/linkedin-archive.py (optional auto-commit).
  when: >
    Use right after publishing to file the content. For drafting a post use /linkedin-post; this skill never drafts, it only archives one piece per invocation.
x-heading-routing:
  category: Content
  triggers:
    - i published this on linkedin
    - linkedin post is live
    - live on linkedin
    - опубликовал на linkedin
    - выложил на linkedin
    - запостил на linkedin
  exclusions:
    - Drafting -> /linkedin-post
    - analytics question -> just answer
    - profile/banner topic -> just answer
  compound: 'No'
  router: auto
---

**Data addresses.** Resources in the data repository are named here by their
logical name, never by a path. Resolve the name before the file operation and
use what it prints:

    .venv/bin/python scripts/data-path.py path <name>

Use `read <name>` for a source that may legitimately be absent, and
`read --required <name>` for one whose absence is a defect. An unknown name
refuses; it never resolves to a plausible directory, and a path written out
here goes stale the day the directory moves.

# LinkedIn Archive

Move a just-published LinkedIn piece from staging (`linkedin-drafts` plus optional explicit images) into the typed datastore archive (`linkedin-archive`, then `{articles|posts|comments}/{slug}/` beneath it).

Script: `scripts/linkedin-archive.py`.

The script looks for the draft at the top level of `linkedin-drafts` and one level into a dated task folder (`YYYY-MM-DD_<slug>/`). It never reads `analytics/`, `reports/` or the archive. A draft found in a task folder takes every file in that folder's own `media/` with it. Those files land flat beside the `.md`, and the emptied task folder is removed.

## Workflow

### 1. Resolve type if needed

If the staged file's name does not contain `_linkedin-(post|article|comment)_` and no `type:` frontmatter is set, ask the CEO via `AskUserQuestion`:

- Post
- Article
- Comment

Pass the answer to the script as `--type <choice>`.

### 2. Resolve images if any

A task-folder draft brings its own `media/` files automatically. Ask only about images from elsewhere. Ask the CEO via `AskUserQuestion`:

- No image
- One image
- More than one

If "One image" or "More than one": send a follow-up plain-text turn, "Reply with the path(s) to the image(s)". Wait for the CEO's free-form reply, one path per line. `AskUserQuestion` is multiple-choice only, so path collection always happens in a separate text turn.

Pass each image as a separate `--image <path>` argument. Default is no image.

### 3. Dry-run

```bash
.venv/bin/python scripts/linkedin-archive.py [--slug <slug>] [--type <type>] [--image <path>]...
```

Exit codes that need handling:

- `2`: nothing found - tell the CEO and stop.
- `3`: destination conflict - surface the existing folder, ask how to proceed.
- `5`: type still ambiguous - re-ask.
- `6`: image path does not exist - re-ask.
- `7`: source untracked - tell CEO to `git add`, then retry.
- `10`: archive root directory missing - report the path the script printed and stop.
- `11`: git could not say whether the source is tracked - report the git error the script printed and stop.
- `12`: two attachments share one file name - show the names, ask which file to keep, and stop.

### 4. Confirm

Show the plan. Ask via `AskUserQuestion`:

- Approve and execute (with auto-commit)
- Approve and execute (no commit)
- Cancel

Wait for explicit approval.

### 5. Execute

```bash
.venv/bin/python scripts/linkedin-archive.py [...flags] --execute [--commit]
```

Exit codes `2`, `3`, `5`, `6`, `7`, `10`, `11` and `12` can fire here too, with the same handling. The execute-only ones are under Undo below: `4` (a move, copy or `git add` failed mid-sequence), `8` (auto-commit failed) and `9` (a git command timed out).

### 6. Confirm done

Report destination folder, files moved (count), and the commit hash if `--commit` was used.

## Undo

The script does not provide an `--undo` flag. To revert a wrong move:

- If `--commit` did NOT fire: `git mv` files back manually, then `rmdir` the empty destination folder. For a task-folder draft, recreate the task folder and its `media/` first.
- If `--commit` fired: the move is one commit in the data repository. Do not rewrite history. Report the commit hash and what `git status` shows to the CEO, and stop.
- If exit code 4: a `git mv` failure leaves nothing moved. A failed image copy or `git add` comes after the `.md` was already moved, so run `git status`, report what was moved and staged, and stop.
- If exit code 8 (`--commit` ran but auto-commit failed): files are moved and staged in the index, but no commit landed. Either inspect the failure (often a pre-commit hook), fix it, and run `git commit` manually; or `git restore --staged <paths>` to unstage and decide what to do next.
- If exit code 9 (git command timed out): a timeout before any `git mv` leaves the disk untouched. If the timeout fires mid-sequence, run `git status` to see what was already moved, and `git mv` back as needed.

## Voice

Concise, operational. No flourish.

## NEVER

- Skip the confirm step.
- Move more than one piece per invocation.
- Use anything other than the script for the actual move.
- Add a trigger phrase that does not contain the word "linkedin" (English or transliterated).
