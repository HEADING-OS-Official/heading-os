# Install HEADING OS

This guide covers the prerequisites, what `install.sh` does, how to verify the result, how to update, and how to fix common problems.

## What you get

HEADING OS is two git repositories in one folder:

```
<target>/
├── engine/   the engine: skills, rules, hooks, scripts. No personal data.
└── data/     your private data repository: everything you create.
```

The default `<target>` is `~/HeadingOS`. The installer writes `HEADING_OS_DATA` into your shell startup files, and the engine finds the data repository through it.

## Supported systems

- Linux.
- Windows 11 through WSL2 with Ubuntu.
- macOS 13 or later.

Windows without WSL is not supported. The installer stops on any other system.

## Prerequisites

You need these accounts:

- An Anthropic account. A Claude subscription is preferred; an API key also works.
- A GitHub account, only when you choose the GitHub backup.

You need these tools. The installer installs `git`, `uv`, Python 3.11 and Node.js when they are missing.

| Tool | Required | Why |
|---|---|---|
| `git` | yes | Clones the engine and versions your data. |
| `uv` | yes | Installs Python 3.11 and the engine's dependencies. |
| Node.js 22 or later | yes | Runs the `npx` bridges some tools use. |
| `gh` (GitHub CLI) | only when you choose the GitHub backup | Creates or adopts your private data repository on GitHub. |
| Claude Code (`claude`) | yes, before your first session | Install it with its official installer. |

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

1. Start Claude Code once: `claude`.
2. Choose a theme.
3. Choose your Claude account.

You do not sign in to GitHub by hand. When you choose the GitHub backup, the installer starts the GitHub sign-in itself in a browser window.

WSL has no default browser. A login prints a URL and a one-time code. Open the URL in your Windows browser and enter the code.

## What you will be asked

You do three things before the first question: install Claude Code, sign in to it, and paste one sentence. From then on you answer in the Claude chat, or in windows the installer opens on your own screen.

1. **All questions first.** Before it installs anything, the installer asks every question the install needs:
   - your mail: Exchange, Google, or connect it later (the default);
   - a private backup of your data on GitHub: yes, or later;
   - your time zone, confirmed from your computer's own setting.
2. **Secrets in a window on your screen.** You type a secret such as your Exchange password into a window on your screen, never into the chat. On a computer without a desktop, you type it into one terminal line the installer prints.
3. **Your computer password, once.** On Linux with a desktop, one system window asks for it. WSL has no such window, so there you type it into one terminal line. The installer tells you before the run starts.
4. **One unattended run.** The installer then installs everything without stopping. For a sign-in or the computer password, it opens the window or the browser page itself. Claude tells you which one to look at.
5. **The finish.** The install ends when every check is green. The installer first tells you what opens next. It then opens a new terminal window with Herdr and Claude in the HEADING OS folder. The setup wizard starts there. If you ran the install inside Herdr, it opens a new Herdr workspace instead. You can then close the terminal where the install ran. Where it cannot open a window, it tells you to open a new terminal and type `heading /setup-wizard`.

## Run the installer

1. Download the installer:

   ```bash
   curl -fsSLO https://raw.githubusercontent.com/HEADING-OS-Official/heading-os/main/install.sh
   ```

2. Run it:

   ```bash
   bash install.sh
   ```

`install.sh` installs `git` and `uv` when they are missing. Then it clones the engine at the latest release tag and runs `scripts/heading-install.py`, which does the rest.

The installer accepts these options:

| Option | Effect |
|---|---|
| `--target DIR` | Puts `engine/` and `data/` under `DIR`. The default is `~/HeadingOS`. |
| `--mail CHOICE` | Your mail: `exchange`, `google`, or `none` to connect it later. |
| `--github-backup yes\|later` | `yes` also keeps a private copy of your data on your GitHub account; an existing private repository with the same name is adopted. `later` keeps it on this computer only. |
| `--tz ZONE` | Your time zone, such as `Europe/Berlin`. |
| `--proxy-signin now\|later` | The Claude sign-in for the model proxy now, or later. |
| `--answers FILE` | Reads the answers from a JSON file (mode 600) and never stops to ask. |
| `--ref TAG` | Installs this tag or branch instead of the latest release. Without it the installer stops when it cannot find a release, rather than falling back to a branch. |
| `--from GIT_URL` | Clones the engine from another git URL. |
| `--dry-run` | Shows what each step would do, and changes nothing. |

Run `bash install.sh --help` to print the same list.


### What the installer does, step by step

Each step prints one line.

1. **Platform and prerequisites.** It detects Linux, WSL2 or macOS, and stops on anything else. On WSL2 it refuses a target under `/mnt` and checks that systemd is on. It installs Python 3.11 through `uv`, and Node.js when it is missing.
2. **Folders.** It creates `<target>/engine` and `<target>/data`.
3. **Engine.** It clones the engine into `<target>/engine` at the newest release tag.
4. **Python environment.** It runs `uv sync --all-extras` in the engine, because every module is part of the install.
5. **Your private data repository.** It runs `scripts/create-data-repo.py` to create `<target>/data` on this machine. When you chose the GitHub backup, it also creates a **private** GitHub repository under your account and pushes. An existing one is adopted. The push checks refuse a data file at a path the engine also holds. The engine has a `README.md`, so the data root carries `ABOUT.md` and never `README.md`.
6. **Data repository layout.** It checks that your data repository has `ABOUT.md` and its thread folders. On a repository made by an older installer it renames `README.md` to `ABOUT.md`. It also creates the missing thread folders and commits only those changes.
7. **Settings and gates.** It copies `.env.example` to `.env` when `.env` does not exist. It arms the commit-time secret scanner, the pre-push gate and the Claude Code session hooks, and confirms each one.
8. **Shell startup files.** It adds `~/.local/bin` to `PATH` and sets `HEADING_OS_DATA`. For bash it writes the login file bash reads (`~/.bash_profile` or `~/.profile`) and `~/.bashrc`. For zsh it writes `~/.zprofile` and `~/.zshrc`.
9. **Check.** It opens a new login shell and confirms that the engine finds your data there.
10. **The finish.** The doctor reads all green. The installer says what opens next. It opens a new window with Herdr and Claude running `/setup-wizard`. Inside Herdr it opens a new workspace instead. Without Herdr it opens a plain Claude window.

The installer is safe to run again. A step that is already in place is skipped, and a broken step is repaired. It records its progress in `<target>/.heading-install/state.json`. It refuses to overwrite a non-empty `data` folder that is not a HEADING OS data repository.

## First session

> **Warning:** `.env` holds secrets. It is gitignored. Never paste its values into chat, issues or tracked files.

> **Warning:** never trust a system folder such as `/mnt/c/WINDOWS/system32` at the trust prompt. Exit, `cd` to the engine, and start `claude` again.

1. The new window the installer opened runs Herdr, and Claude with `/setup-wizard` inside it. Where no window opened, open a new terminal and type `heading /setup-wizard`. The wizard asks about you and your business and writes your voice, business and personal documents.
2. Run `/prime`. It loads context and reports problems.

You never edit `.env` by hand: the installer writes your answers there, and each secret goes in through a window on your screen.

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

Run each check from the engine directory, in a terminal opened after the install. An older terminal has no `HEADING_OS_DATA`, so check 5 prints the wrong folder.

1. Check the session hooks: `bash scripts/setup-platform.sh --check`. It exits non-zero and names each missing hook.
2. Check the commit gate: `uv run python scripts/install-hooks.py --check`.
3. Check the git hooks: `uv run python scripts/install-git-hooks.py --check`.
4. Check general health: `uv run python scripts/workspace-health.py`. Most lines should read OK.
5. Check the data root:

   ```bash
   uv run python -c "from scripts.utils.paths import get_data_root; print(get_data_root())"
   ```

   It must print your `<target>/data` path.

6. Start `claude` and run `/prime`. A fresh data repository looks sparse. That is expected.

The three `--check` commands confirm that a hook is registered or installed. They do not prove that a hook ran.

## Update

Updates arrive as release tags.

1. Go to the engine: `cd <target>/engine`.
2. Make sure `git status` shows no changes of your own.
3. Fetch the tags: `git fetch --tags origin`.
4. Check out the new tag: `git checkout <tag>`.
5. Update the dependencies: `uv sync`.
6. Re-arm the session hooks: `bash scripts/setup-platform.sh`. It merges and keeps your local settings.
7. Confirm: `bash scripts/setup-platform.sh --check`.

Running `bash install.sh` again repairs an install, but does not move the engine to a newer tag.

Your data repository is yours. Update it with your own `git pull` and `git push`, or with `/backup`.

## Back up your data

- Run `/backup` in a session, or run `uv run python scripts/push-all.py`.
- To preview without changes, run `uv run python scripts/push-all.py --dry-run`.

`push-all.py` needs `GH_TOKEN` in `.env` (a `--dry-run` runs without it). It commits each repository's changes locally first and then pushes `main` to `origin`. A data repository created without `--github` has no `origin`, so its changes are committed locally, and its push fails and is reported. To back it up off the machine, give it a private remote once, then use `/backup` as above:

```bash
gh repo create <name> --private --source <target>/data --remote origin --push
```

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Linux commands fail with `'&&' is not a valid statement separator` | You are in PowerShell. Run `wsl -d Ubuntu` and work there. |
| `python: command not found` | Ubuntu ships `python3` only. Use `uv run python ...`. Never use system pip. |
| `uv: command not found` or `claude: command not found` | The terminal opened before the install. Run `source ~/.bashrc` (zsh: `source ~/.zshrc`), or open a new terminal. |
| `Python 3.11 not found`, or the wrong Python | Run `uv python install 3.11`, then `uv sync`. Check with `uv run python --version`. |
| `which claude` or `which node` shows `/mnt/c/...` | The shell uses the Windows install. Run `. "$NVM_DIR/nvm.sh"` and check again. |
| `gh is not signed in` | Answer the GitHub backup later: run the installer again with `--github-backup later`. |
| `keep HEADING OS on the Linux filesystem` | Choose a target under `~`, for example with `--target ~/HeadingOS`. |
| `has uncommitted changes` | Commit or set aside your engine changes, then re-run. |
| `is not empty and is not a HEADING OS data repository` | Move that directory aside, then re-run. |
| No hook fires: no session brief, no PreToolUse wall | `setup-platform.sh` did not run. Run `bash scripts/setup-platform.sh`, then `--check`. |
| A commit with a secret goes through | The commit gate is not installed. Run `uv run pre-commit install`, then `uv run python scripts/install-hooks.py --check`. |
| `get_data_root()` prints a path under `.../examples` | The engine cannot find the data repository: this shell has no `HEADING_OS_DATA`. Open a new terminal, or run `bash install.sh` again to repair the shell startup files. |
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

After the install, run .venv/bin/python scripts/heading-console.py open in ~/HeadingOS/engine to see every web interface and the guides.

## Modules

Every install includes the modules below, and none of them is optional. Each has its own guide in this folder. `YARDS.md` explains the working method that uses them:

- `HINDSIGHT.md`: long-term memory server.
- `LIFIC.md`: local issue tracker.
- `HERDR.md`: terminal multiplexer for parallel work in yards.
- `CLIPROXY.md`: one local proxy for ChatGPT, Kimi, Gemini and Grok.
- `AGENTVIEW.md`: subagent panel, proxy token dashboard, and AgentsView.
- `YARDS.md`: the HELM and YARD working method.
