# CLAUDE.md: HEADING OS

HEADING OS is an operations engine for an AI executive assistant: skills, always-on rules, automation scripts and hooks that let Claude Code act as a strategic assistant across sessions. This repository is the **engine**: code only, no data. Your data lives in a second, private repository beside it.

## Engine and data

The engine never contains real data, secrets or personal information. Everything that belongs to the person using it (contacts, knowledge, outputs, threads, identity, credentials) lives in the private data repository `.heading-os-data` beside this one, and in gitignored runtime files (`.env`, `.sessions`). The engine finds the data repository as its sibling directory, or through `HEADING_OS_DATA` in `.env`.

Every artifact you create is written into the data repository at the address its kind has, resolved by logical name: `python scripts/data-path.py path <name>` from a shell or a skill, `data_path()` in Python. Never build a path to a data file by hand, and never write an artifact into the engine tree. `config/routing-map.yaml`, `scripts/leak-guard.py` and the push-time content scan in `scripts/push-all.py` refuse a data file in the engine.

## Layout

- `.claude/skills/`: skills, one folder each (`SKILL.md`, optional `references/`, `triggers.json`)
- `.claude/rules/`: always-on and path-scoped behavioural rules
- `.claude/hooks/`: the session hooks that enforce the rules
- `scripts/`: CLI utilities; shared modules in `scripts/utils/`
- `config/`: engine configuration; per-instance `*.example.*` templates sit beside the scripts that read them
- `reference/`: procedure files a rule or skill loads
- `docs/`: the install guide, the user guides (`WORKING-WITH-HEADING-OS.md`, `SKILLS.md`, `RULES-AND-PROTECTIONS.md`, `WHATS-NEW.md`) and the optional-tool guides
- `tests/`: the guarantees that keep data out of the engine

## Setup

`install.sh` does all of it; `docs/INSTALL.md` explains each step. By hand:

1. `uv sync`
2. `cp .env.example .env`, then fill in your own keys. Never commit `.env`.
3. `pre-commit install`, once per clone, or the commit gates are not armed.
4. `.venv/bin/python scripts/install-git-hooks.py`, once per clone, for the pre-push gate.
5. `bash scripts/setup-platform.sh`, once per clone. **A clone that skips this runs with its session walls down.** `bash scripts/setup-platform.sh --check` reports the state.

Run scripts with `.venv/bin/python`, never a bare `python`, so a machine-wide interpreter without the pinned dependencies cannot run them.

## Working in parallel: HELM and YARD

One main session works on `main` in this clone: it is HELM, and everything live happens there (background services, sending, pushing, merging). Each separate piece of work runs in a YARD, an isolated git worktree on its own branch, created through Herdr. A yard commits only on the operator's own word typed in that yard; HELM reviews and merges the branch, then the worktree is deleted. The full method: `docs/YARDS.md`.

## Design records the engine points at

Some rules, skills and script docstrings point at design records in the data repository under the registry names `spec-docs` and `reference-docs` (`ARCHITECTURE.md`, `SECURITY-MODEL.md`, `HOOKS-REFERENCE.md` and others). Those are the maintainer's own records and are not part of this distribution, so in your data repository the names resolve to empty directories. The rule or code that points at one is complete without it; the record only explains why it was written that way. Write your own records there if you want them.

## Optional tools

Hindsight (memory), Lific (issue tracker), Herdr (parallel sessions), CLIProxyAPI (other models) and AgentView (activity view and proxy usage). The core runs without any of them; each guide is in `docs/`.

## Security

Read `SECURITY.md` first. Keep secrets and personal data out of every commit: the commit hooks and the push-time scan are built to refuse them, but the first line of defence is you. Every outbound send is drafted and approved by a person; nothing sends on its own.
