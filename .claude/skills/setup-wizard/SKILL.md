---
name: setup-wizard
disable-model-invocation: true
description: Interactive setup wizard for a public HEADING OS clone. Walks the user through 16 questions, enriches short answers into rich docs, never takes a secret in the chat (the user types it into a hidden terminal prompt), and produces a completion-% dashboard on re-runs. Refuses to run on the CEO master workspace. EXPLICIT INVOCATION ONLY - mutates the workspace skeleton (the personalized target files the apply script names).
argument-hint: "(no arguments)"
allowed-tools: "Read, Bash(.venv/bin/python:*)"
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "1.2"
x-heading-capability:
  what: >
    Walks a fresh public HEADING OS clone through an interactive per-question
    setup of 16 questions, enriches short answers
    into full voice/personal/business docs, sends every secret to a hidden
    terminal prompt (scripts/heading-secrets.py) instead of the chat,
    and shows a completion-percent dashboard on re-runs.
  how: >
    Explicit invocation only - type /setup-wizard. All writes go through
    scripts/apply-wizard-answers.py into .setup/answers.json and the personalized
    target files. It refuses to run on the CEO master workspace.
  when: >
    Use on a public HEADING OS clone to personalize it, or re-run anytime to
    edit answers. Not for the CEO master workspace.
x-heading-routing:
  category: Operations
  triggers:
    - NEVER auto-trigger. Explicit `/setup-wizard` only.
  exclusions:
    - Refuses to run on CEO master workspace
    - intended for public HEADING OS clones
  router: manual
---
# Setup Wizard

This skill runs an interactive, per-question setup flow that personalizes a fresh workspace. See `superpowers/specs/2026-04-24-setup-wizard-design.md` under `spec-docs` in the data repository (resolve with `.venv/bin/python scripts/data-path.py path spec-docs`) for the full contract.

## Preconditions

Before starting, Claude MUST:

1. Run `.venv/bin/python scripts/apply-wizard-answers.py --status` to detect the workspace and current completion state.
2. If exit code is `4` (ceo-master detected), print the abort message from stderr AND stop immediately. Do not proceed. Do not offer `--force-ceo-master` to the end user; it is a test-only override.
3. If exit code is `1`, surface the schema error and stop. An identity file carrying the retired `exec-workspace` type lands here as a schema error.

## Source of Truth

**All question data (prompt, example, help, type, id) is returned by `--status` in each `rows` entry.** Use those fields directly. Do NOT `cat`, `Read`, or otherwise open `wizard-questions.yaml` yourself; the apply script resolves it and is the one reader.

## First-Run Flow (empty answers.json)

Announce:

> Welcome. I'll ask you ~N questions to set up your workspace. After each one I'll show you an example answer, where the question bank has one, so you know what I'm looking for.
>
> Rules:
>   - Type an answer and press Enter.
>   - Type `skip` if you don't want to answer - you can come back later.
>   - Type `example` if you want me to read the example again.
>   - Type `help` if you want me to explain what the question is for.
>   - Nothing is saved until you confirm each answer.
>
> Ready? Let's start.

Then walk through each question from the `--status` rows where `status` is `pending`, in bank order.

## Per-Question Flow

Render each question:

```text
Q<N> of <TOTAL> - <label>
---------------------------
<prompt>

  Example: <example>    (omit this line when the example is empty)

Your answer: _
```

Wait for user input. Branch by type.

### Placeholder or List

- `skip` -> `.venv/bin/python scripts/apply-wizard-answers.py --skip <id>` -> confirm -> next.
- `example` -> re-render example -> stay.
- `help` -> render the question's `help` field -> stay.
- Any other -> invoke:

```bash
.venv/bin/python scripts/apply-wizard-answers.py --question <id> --value-from-stdin
```

with JSON on stdin: `{"value": "<text>"}` (placeholder) or `{"value": ["a","b","c"]}` (list, split on comma).

### Rich

After the user's short answer:

1. Announce: `Drafting your <doc name>...`
2. Compose a full document inline (200-400 words for voice/personal/business; 100-200 for calendar).
3. Display the draft in a visible block.
4. Offer the choices below:

```text
Does this sound like you?
  [y] yes, save it
  [t] mostly - I'll tell you what to tweak
  [r] no, let me give you a different short answer and redraft
  [s] skip this question for now
```

Branches:

- `y`: invoke apply with `{"value":"<short>","draft":"<full>","draft_approved":true}`. Confirm. Advance.
- `t`: ask "What should I change?", read tweak notes, re-render inline, show the same four-choice prompt.
- `r`: invoke apply with `{"archive_draft":true}` to preserve current draft into `draft_previous`, then re-ask the short question prefilling the user's previous short answer.
- `s`: invoke `apply-wizard-answers.py --skip <id>`. Advance.

### Secret

**Never ask for a secret in the chat, and never accept one.** A key typed here lands in the session transcript. If the user pastes a key anyway, do not repeat it and do not pass it to any command. Tell them it is now in this conversation, and that they should replace it with a new key from its provider.

1. Render the prompt and the help line. The prompt names the exact command and the variable name.
2. Tell the user to open a terminal in the engine folder and run that command, of this form:

```bash
.venv/bin/python scripts/heading-secrets.py set <NAME>
```

3. Wait for the user to type `done` or `skip`.
4. On `skip`, invoke `apply-wizard-answers.py --skip <id>`. Advance.
5. On `done`, invoke apply with `{"env_set": true}`. The apply script checks that the name is set in `.env` and stores no value.
6. If the apply script reports that the name is not set, show its message. Then go back to step 2.

Exit code `1` with "a secret is never passed to the wizard" means a value was sent. Correct the payload to `{"env_set": true}`.

## Re-Run Flow (dashboard)

If any row has `status=answered`, render the dashboard directly:

```text
----------------------------------------------------------
 HEADING OS Setup Wizard - Welcome back, <name>
----------------------------------------------------------
 Progress: <req.answered> of <req.total> required - <completion_pct>%

 Required
  <numbered rows with [x]/[-]/[ ] glyph + display_value>

 Optional (don't affect %)
  <rows>

 What do you want to do?
   [number] - edit that answer
   'all'    - walk through every question again
   'apply'  - re-apply current answers to files
   'done'   - exit

 > _
```

Commands:

- `<number>`: find row, run per-question flow, re-render dashboard.
- `all`: walk all questions in bank order as re-edit.
- `apply`: run `.venv/bin/python scripts/apply-wizard-answers.py --all`. Re-render.
- `done`: print one-line summary with full path to `.setup/answers.json`. Exit.
- `help`: re-print commands.

## What This Skill Does NOT Do

- Does NOT invoke other skills.
- Does NOT write to `.setup/answers.json` directly. All writes go through `apply-wizard-answers.py`.
- Does NOT make network calls.
- Does NOT ask for, read or pass a secret value. Secrets go only through `scripts/heading-secrets.py set` in the user's terminal.
- Does NOT accept `--force-ceo-master`. If audience detection reports ceo-master, abort.

## Files Touched (indirectly)

- Reads: `.setup/answers.json` (via `--status`). Question bank content is delivered inside `--status` rows; Claude never reads the YAML directly.
- Writes (via apply script): `.setup/answers.json`, personalized target files per question. The user's own `heading-secrets.py set` writes `.env`; the wizard never does.
  Read the per-question target paths `apply-wizard-answers.py --status` reports. Assume none of them.

## Completion

On 100% completion, print:

> Setup complete. Your workspace is ready. You can run `/setup-wizard` anytime to edit answers.
