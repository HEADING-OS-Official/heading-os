# Install HEADING OS

This guide covers the prerequisites, what `install.sh` does, how to verify the result, how to update, and how to fix common problems.

## What you get

HEADING OS is two git repositories side by side under one parent directory:

```
<parent>/
├── .heading-os/        the engine: skills, rules, hooks, scripts. No personal data.
└── .heading-os-data/   your private data repository: everything you create.
```

The engine finds the data repository because the two sit side by side. The default `<parent>` is `~/ai/claude-workspaces`.

## Supported systems

- Linux.
- Windows 11 through WSL2 with Ubuntu.
- macOS 13 or later.

Windows without WSL is not supported. The installer stops on any other system.

## Prerequisites

You need these accounts:

- A GitHub account. The installer creates your private data repository there.
- An Anthropic account. A Claude subscription is preferred; an API key also works.

You need these tools. The installer checks for each one and stops if a required tool is missing.

| Tool | Required | Why |
|---|---|---|
| `git` | yes | Clones the engine and versions your data. |
| `uv` | yes | Installs Python 3.11 and the engine's dependencies. |
| `gh` (GitHub CLI) | yes, unless you use `--local-data` | Creates your private data repository on GitHub. |
| Node.js | yes, for Claude Code | Claude Code installs through `npm`. |
| Claude Code (`claude`) | yes, before your first session | The installer warns, but does not stop, when it is missing. |

### Windows only: set up WSL2

Run these steps in an Administrator PowerShell window.

1. Run `wsl --install`.
2. Reboot when Windows asks.
3. Create a Linux user and password on the first launch.
4. Open Ubuntu with `wsl -d Ubuntu`.

Run every later command inside Ubuntu, not in PowerShell.

> **Warning:** keep both repositories on the Linux filesystem (`~/...`), never under `/mnt/c`. The installer refuses a parent directory under `/mnt`. The Windows mount is much slower for git and Python.

### Install the tools on Ubuntu or WSL2

1. Install the system packages:

   ```bash
   sudo apt update && sudo apt install -y build-essential curl git ca-certificates gh
   ```

2. Install nvm:

   ```bash
   curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
   ```

3. Load nvm into this shell:

   ```bash
   export NVM_DIR="$HOME/.nvm" && . "$NVM_DIR/nvm.sh"
   ```

4. Install Node.js: `nvm install --lts`.
5. Install uv:

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

6. Put uv on your PATH: `export PATH="$HOME/.local/bin:$PATH"`.
7. Confirm uv works: `uv --version`.
8. Install Claude Code: `npm install -g @anthropic-ai/claude-code`.
9. Confirm the Linux build of Claude Code: `which claude`.

On WSL2, `which claude` must point under `~/.nvm/...`. A path under `/mnt/c/...` is the Windows build, and it cannot see your Linux files. Re-load nvm (step 3) and check again.

On macOS, install Homebrew from https://brew.sh first, then install `git` and `gh` with `brew install git gh` in place of step 1. Steps 2 to 9 are the same.

### Sign in

1. Sign in to GitHub: `gh auth login`. Choose GitHub.com, HTTPS, and a web browser login.
2. Start Claude Code once: `claude`.
3. Choose a theme.
4. Choose your Claude account.

WSL has no default browser. Both logins print a URL and a one-time code. Open the URL in your Windows browser and enter the code.

## Run the installer

1. Clone the engine into its final place:

   ```bash
   git clone https://github.com/HEADING-OS-Official/heading-os.git ~/ai/claude-workspaces/.heading-os
   ```

2. Run the installer from the clone:

   ```bash
   bash ~/ai/claude-workspaces/.heading-os/install.sh
   ```

The installer finds the engine already cloned, moves it to the latest release tag, and continues from there.

The installer accepts these options:

| Option | Effect |
|---|---|
| `--dir PARENT` | Puts both repositories under `PARENT`. The default is `~/ai/claude-workspaces`. |
| `--local-data` | Creates the data repository on this machine only, with no GitHub repository. `gh` is then not required. |
| `--ref TAG` | Installs this tag or branch instead of the latest release. Without it the installer stops when it cannot find a release, rather than falling back to a branch. |
| `--from GIT_URL` | Clones the engine from another git URL. |

Run `bash install.sh --help` to print the same list.


### What the installer does, step by step

1. **Platform.** It detects Linux, WSL2 or macOS, and stops on anything else. On WSL2 it refuses a parent directory under `/mnt`.
2. **Tools.** It checks `git` and `uv`. Without `--local-data` it also checks `gh` and that `gh` is signed in. It warns when `claude` is missing.
3. **Engine.** It finds the newest release tag (tags that start with `v`). It clones the engine into `<parent>/.heading-os` at that tag. If the engine is already cloned and has no uncommitted changes, it fetches the tags and checks out that tag.
4. **Python environment.** It runs `uv sync` in the engine. This installs the core dependencies into `<parent>/.heading-os/.venv`.
5. **Your private data repository.** It runs `scripts/create-data-repo.py` to create `<parent>/.heading-os-data`. That script builds the empty data tree, writes a `.gitignore` and a README, and makes the first commit. Without `--local-data` it also creates a **private** GitHub repository under your account, sets it as `origin`, and pushes. With `--local-data` it skips GitHub.
6. **Settings and gates.** It copies `.env.example` to `.env` when `.env` does not exist. It arms the commit-time secret scanner with `pre-commit install`, and the pre-push gate with `scripts/install-git-hooks.py`. It registers the Claude Code session hooks with `scripts/setup-platform.sh`, and confirms them with `--check`.
7. **Check.** It confirms that the engine resolves its data root to `<parent>/.heading-os-data`.

The installer is safe to run again. Each step that is already done is skipped. It refuses to overwrite a non-empty `.heading-os-data` that is not a HEADING OS data repository.

### Optional extras

`uv sync` installs the core only. Heavy integrations are optional extras: `email`, `telegram`, `browser`, `documents`, `media`, `dashboard`, `ai-extra`, `observability`, `research`, and `all`.

- To add one extra, run `uv sync --extra <name>` in the engine.
- To add all extras, run `uv sync --all-extras`.

A script that needs a missing extra prints the exact `uv sync --extra <name>` command.

## First session

> **Warning:** `.env` holds secrets. It is gitignored. Never paste its values into chat, issues or tracked files.

> **Warning:** never trust a system folder such as `/mnt/c/WINDOWS/system32` at the trust prompt. Exit, `cd` to the engine, and start `claude` again.

1. Go to the engine: `cd <parent>/.heading-os`.
2. Open `.env` in an editor.
3. Fill in the keys you have. Leave the others empty.
4. Start Claude Code: `claude`.
5. Trust this folder when Claude asks.
6. Run `/setup-wizard`. It asks questions and writes your voice, business and personal documents.
7. Run `/prime`. It loads context and reports problems.

What to do next, and how an ordinary day works: `WORKING-WITH-HEADING-OS.md`. Every skill: `SKILLS.md`.

### Plugins

The engine enables some Claude Code plugins in `.claude/settings.json`. Enabling a plugin there does not install it.

1. Add the official marketplace once:

   ```bash
   claude plugin marketplace add anthropics/claude-plugins-official
   ```

2. Open `.claude/settings.json` and read `enabledPlugins`.
3. For each entry set to `true`, run one install command, for example:

   ```bash
   claude plugin install superpowers@claude-plugins-official --scope project
   ```

Paste one install line at a time. Never use `@latest`; the form is always `<plugin>@<marketplace>`.

## Verify the install

Run each check from the engine directory.

1. Check the session hooks: `bash scripts/setup-platform.sh --check`. It exits non-zero and names each missing hook.
2. Check the commit gate: `uv run python scripts/install-hooks.py --check`.
3. Check the git hooks: `uv run python scripts/install-git-hooks.py --check`.
4. Check general health: `uv run python scripts/workspace-health.py`. Most lines should read OK.
5. Check the data root:

   ```bash
   uv run python -c "from scripts.utils.paths import get_data_root; print(get_data_root())"
   ```

   It must print your `.heading-os-data` path.

6. Start `claude` and run `/prime`. A fresh data repository looks sparse. That is expected.

The three `--check` commands confirm that a hook is registered or installed. They do not prove that a hook ran.

## Update

Updates arrive as release tags.

1. Go to the engine: `cd <parent>/.heading-os`.
2. Make sure `git status` shows no changes of your own.
3. Fetch the tags: `git fetch --tags origin`.
4. Check out the new tag: `git checkout <tag>`.
5. Update the dependencies: `uv sync`.
6. Re-arm the session hooks: `bash scripts/setup-platform.sh`. It merges and keeps your local settings.
7. Confirm: `bash scripts/setup-platform.sh --check`.

You can also run `bash install.sh` again. It checks out the latest release tag when the engine has no uncommitted changes.

Your data repository is yours. Update it with your own `git pull` and `git push`, or with `/backup`.

## Back up your data

- Run `/backup` in a session, or run `uv run python scripts/push-all.py`.
- To preview without changes, run `uv run python scripts/push-all.py --dry-run`.

`push-all.py` needs `GH_TOKEN` in `.env` (a `--dry-run` runs without it). It commits each repository's changes locally first and then pushes `main` to `origin`. A data repository created with `--local-data` has no `origin`, so its changes are committed locally, and its push fails and is reported. To back it up off the machine, give it a private remote once, then use `/backup` as above:

```bash
gh repo create <name> --private --source <parent>/.heading-os-data --remote origin --push
```

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Linux commands fail with `'&&' is not a valid statement separator` | You are in PowerShell. Run `wsl -d Ubuntu` and work there. |
| `python: command not found` | Ubuntu ships `python3` only. Use `uv run python ...`. Never use system pip. |
| `uv: command not found` | `uv` is in `~/.local/bin`. Run `source $HOME/.local/bin/env`, or open a new shell. |
| `Python 3.11 not found`, or the wrong Python | Run `uv python install 3.11`, then `uv sync`. Check with `uv run python --version`. |
| `which claude` or `which node` shows `/mnt/c/...` | The shell uses the Windows install. Run `. "$NVM_DIR/nvm.sh"` and check again. |
| `gh is not signed in` | Run `gh auth login`, or re-run the installer with `--local-data`. |
| `keep HEADING OS on the Linux filesystem` | Choose a parent directory under `~`, for example with `--dir ~/ai/claude-workspaces`. |
| `has uncommitted changes` | Commit or set aside your engine changes, then re-run. |
| `exists, is not empty and is not a HEADING OS data repository` | Move that directory aside, then re-run. |
| No hook fires: no session brief, no PreToolUse wall | `setup-platform.sh` did not run. Run `bash scripts/setup-platform.sh`, then `--check`. |
| A commit with a secret goes through | The commit gate is not installed. Run `uv run pre-commit install`, then `uv run python scripts/install-hooks.py --check`. |
| `get_data_root()` prints a path under `.../examples` | The engine cannot find the data repository. Set `HEADING_OS_DATA=/absolute/path/to/.heading-os-data` in `.env`. |
| Plugin install says `not found in marketplace latest` | Do not use `@latest`. Install `<plugin>@claude-plugins-official`. |
| `Plugin "" not found in marketplace` or `syntax error near unexpected token` after a paste | A pasted multi-line block broke. Paste one line at a time. |
| `No marketplaces configured` | Run `claude plugin marketplace add anthropics/claude-plugins-official` first. |
| A timer installer says `systemctl not found` (WSL2) | Enable systemd: add a `[boot]` section with `systemd=true` to `/etc/wsl.conf`. |

> **Warning:** when an error touches the data location or git history, stop. Copy the terminal text before you act. Never guess past it.

## Guides for daily use

- `WORKING-WITH-HEADING-OS.md`: the first day, an ordinary day, approval and sending, handoffs, backups.
- `SKILLS.md`: every skill, with one page per group.
- `RULES-AND-PROTECTIONS.md`: the always-on rules and the checks that refuse an action.
- `WHATS-NEW.md`: what changed in each release.

## Optional tools

The core runs without any of these. Each has its own guide in this folder:

- `HINDSIGHT.md`: long-term memory server.
- `LIFIC.md`: local issue tracker.
- `HERDR.md`: terminal multiplexer for parallel work in yards.
- `CLIPROXY.md`: one local proxy for ChatGPT, Kimi, Gemini and Grok.
- `AGENTVIEW.md`: subagent panel, proxy token dashboard, and AgentsView.
- `YARDS.md`: the HELM and YARD working method.
