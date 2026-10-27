# Herdr: run several assistants at once

This guide takes you from a machine with no Herdr on it to a working setup where HEADING OS opens a fresh, safe copy of itself for every task. It covers installing Herdr, setting it up, connecting it to HEADING OS, using it every day, checking that it works, fixing common problems, and updating or removing it.

Herdr is optional. HEADING OS runs without it. You need Herdr only if you want to work on several tasks at the same time, each in its own copy. The way of working that Herdr supports is explained in `YARDS.md`. Read this guide first, then that one.

## What Herdr is, and why HEADING OS uses it

Herdr is a terminal program for coding assistants. Its home page is https://herdr.dev and its licence is Apache 2.0.

A few words used in this guide:

- **Terminal**: the text window where you type commands.
- **Pane**: one terminal area inside Herdr. Herdr shows several panes side by side.
- **Workspace**: a group of panes for one project or one task. Herdr lists workspaces in a sidebar on the left.
- **Server**: Herdr keeps running in the background after you close the window. That background part is the server. Your panes live inside it, so nothing is lost when you close the window.
- **Git worktree**: a second working copy of the same git repository, on its own branch, in its own folder. Two worktrees of one repository can hold different changes at the same time without touching each other.
- **Plugin**: a small add-on that Herdr runs when something happens, for example when a new worktree is created.

HEADING OS has one main copy of the engine, called **HELM**, and short-lived task copies, called **yards**. A yard is a git worktree. Herdr is what creates each yard, opens it in its own workspace, and starts Claude Code inside it. HEADING OS ships a Herdr plugin that prepares every new yard and proves that the safety checks work in it before Claude starts.

HEADING OS uses Herdr in three places:

| Part of HEADING OS | What it does with Herdr |
|---|---|
| The yard plugin, in `scripts/herdr/heading-os-yard/` | Prepares each new yard when Herdr creates it. |
| `scripts/herdr-brief.py` | Sends a task description (a "brief") from HELM into a yard's Claude session. |
| `scripts/utils/herdr_agent.py` | Reads Herdr's list of running assistants, labels panes, and lets `/second-wind` clear or compact a long session in its own pane. |

## What you need first

Before you start, have these ready:

1. HEADING OS installed and working, as described in `INSTALL.md`. You need the engine folder (`.heading-os`) and your data folder (`.heading-os-data`) side by side under one parent folder. This guide calls that parent folder `<parent>`. The installer's default is `~/ai/claude-workspaces`.
2. A Linux machine, Windows 11 with WSL2 (Ubuntu), or a Mac. Herdr's install script accepts Linux and macOS on `x86_64` or `arm64`/`aarch64` processors. On Windows, run everything in this guide inside Ubuntu (WSL2), never in PowerShell.
3. These tools on your `PATH` (the list of folders your terminal searches for programs):
   - `curl` and `awk`. The Herdr install script stops if either is missing.
   - One of `sha256sum`, `shasum` or `openssl`. The install script uses it to check the download.
   - `git`, `uv` and `python3`. The yard plugin needs all three. `INSTALL.md` already installed them.
   - Claude Code (`claude`). `INSTALL.md` already installed it.

Check them in one go:

```bash
command -v curl awk git uv python3 claude
```

Each tool that is installed prints its path. A missing tool prints nothing. Install anything missing before you continue.

## Install Herdr

### Linux and WSL2 (Ubuntu)

1. Open your terminal. On Windows, open Ubuntu.
2. Run the official install script:

   ```bash
   curl -fsSL https://herdr.dev/install.sh | sh
   ```

   The script detects your system, downloads the right file, checks its SHA-256 fingerprint, and puts the program at `~/.local/bin/herdr`. To put it somewhere else, set `HERDR_INSTALL_DIR` first, for example `HERDR_INSTALL_DIR=~/bin`, on the same line before `sh`.
3. If the script prints `~/.local/bin is not in your PATH`, add that folder to your PATH. For bash, run:

   ```bash
   echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
   source ~/.bashrc
   ```

4. Confirm Herdr is found:

   ```bash
   herdr --version
   ```

   It prints a line such as `herdr 0.9.5`. The HEADING OS plugin needs version `0.8.0` or later.

### macOS

1. Open the Terminal app.
2. Run the same install script:

   ```bash
   curl -fsSL https://herdr.dev/install.sh | sh
   ```

3. If the script warns that `~/.local/bin` is not in your PATH, add it. The default shell on macOS is zsh:

   ```bash
   echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
   source ~/.zshrc
   ```

4. Confirm it: `herdr --version`.

You can also install Herdr on macOS with Homebrew: `brew install herdr`.

### Other ways to install

Herdr's install page, https://herdr.dev/docs/install/, also lists `mise use -g herdr`, a Nix flake, and a manual download. Any of them works with HEADING OS, as long as `herdr --version` prints `0.8.0` or later afterwards.

## First-time setup

### 1. Start Herdr once

1. Go to your engine folder:

   ```bash
   cd <parent>/.heading-os
   ```

2. Start Herdr:

   ```bash
   herdr
   ```

   The first start shows a short welcome screen that asks how you want notifications. Answer it, and you land in a workspace for this folder.
3. Learn the two keys you need most. Herdr's "prefix" key is `ctrl+b`. Press `ctrl+b`, let go, then press the next key.
   - `ctrl+b` then `q`: leave Herdr. Your panes keep running in the background.
   - `herdr` typed again in any terminal: come back to the same panes.

   You can also use the mouse: click panes and tabs, drag the borders between panes, and right-click for menus.

### 2. Let Herdr see Claude Code

Herdr can show in its sidebar whether Claude is working, waiting or done. For that it installs a small hook into Claude Code:

```bash
herdr integration install claude
```

Check it:

```bash
herdr integration status
```

The `claude:` line must say `current`.

### 3. Find your config file

Herdr works without a config file. When you want one, it is `~/.config/herdr/config.toml` on both Linux and macOS. `herdr --help` prints the exact path on its `Config:` line. To use a different file, set `HERDR_CONFIG_PATH`.

To see every setting with its default value, print the default config:

```bash
herdr --default-config
```

To start your own file from those defaults:

```bash
mkdir -p ~/.config/herdr
herdr --default-config > ~/.config/herdr/config.toml
```

Skip the second command if the file already exists, or it is overwritten.

### 4. Choose where yards are created

Herdr puts every new worktree under one folder. The setting is `directory` in the `[worktrees]` section. Its default is `~/.herdr/worktrees`. Herdr lays each checkout out as `<directory>/<repository name>/<branch name>`.

The one rule: **that folder must be outside the engine folder.** A yard inside the engine folder would be a second copy sitting inside the first, and HEADING OS's checks could look at the wrong copy.

The default is already outside, so you can leave it. If you prefer your yards next to your two repositories, set it like this in `config.toml`:

```toml
[worktrees]
directory = "~/ai/claude-workspaces/.yard"
```

Use your own `<parent>` folder in place of `~/ai/claude-workspaces`.

### 5. Turn on pop-up notices

The yard plugin tells you when a yard is ready or broken through Herdr notices. Herdr's default for notices is `off`, so you would see nothing. Turn them on in `config.toml`:

```toml
[ui.toast]
delivery = "herdr"
```

`herdr` shows notices inside Herdr. The other accepted values are `terminal` (ask your terminal app to show a desktop notice), `system` (ask the operating system directly) and `off`.

### 6. Check and apply the config

1. Check the file for mistakes:

   ```bash
   herdr config check
   ```

   It prints `config: ok` when the file is valid.
2. Tell the running server to read it again, without closing any pane:

   ```bash
   herdr server reload-config
   ```

   The answer contains `"status":"applied"`. If it lists diagnostics, a section or key name is wrong. Fix it and run the command again.

## Connect Herdr to HEADING OS

### 1. Link the yard plugin

Link the plugin from your main engine copy (HELM). Use the full path, not a relative one:

```bash
herdr plugin link <parent>/.heading-os/scripts/herdr/heading-os-yard
```

`link` registers the folder where it is. Nothing is copied. Herdr then runs the plugin's scripts straight from your engine folder, so an update of the engine updates the scripts too.

Do not copy the folder somewhere else and link the copy. The copy would keep running old scripts after every engine update.

### 2. Confirm the plugin is registered

```bash
herdr plugin list
```

You should see a line that starts with `heading-os.yard` and says `enabled`. To see exactly what Herdr registered, ask for the full record:

```bash
herdr plugin list --plugin heading-os.yard --json
```

Its `events` list must contain three entries: `worktree.created`, `worktree.opened` and `worktree.removed`. Its `actions` list must contain `doctor`.

The plugin reacts to these Herdr events:

| Event or action | What runs | What it does |
|---|---|---|
| `worktree.created` | `yard-bootstrap.sh` | Prepares a new yard. |
| `worktree.opened` | `yard-bootstrap.sh` | Prepares a yard that was opened without being created through Herdr. On a yard that is already ready, it only reads one small status file. |
| `worktree.removed` | `yard-teardown.sh` | Removes the deleted yard's scratch folder in your data repository, but only when its branch is already merged into `main`. |
| Action `doctor` | `yard-bootstrap.sh --doctor-only` | Re-checks an existing yard without preparing it again. |

**Important:** Herdr reads the list of events once, when you link the plugin. If an engine update changes the events in `scripts/herdr/heading-os-yard/herdr-plugin.toml`, link the plugin again, or the new events are never used:

```bash
herdr plugin unlink heading-os.yard
herdr plugin link <parent>/.heading-os/scripts/herdr/heading-os-yard
herdr plugin list --plugin heading-os.yard --json
```

### 3. Protect your data repository from commits made in yards

Yards may write files into your data repository, but they must never run `git commit` there. That repository has one shared git index, so a commit from one task would sweep up other tasks' unfinished files. A small git hook refuses such commits. Install it once, from HELM:

```bash
cd <parent>/.heading-os
.venv/bin/python scripts/install-data-overlay-guard.py
```

It refuses to overwrite a `pre-commit` hook you already have. In that case it prints the lines to add to your hook by hand. To see the hook's state without changing anything:

```bash
.venv/bin/python scripts/install-data-overlay-guard.py --check
```

This script runs only in HELM. In a yard it stops with a message that names HELM.

### 4. Optional: change how the plugin starts Claude

When a yard is ready, the plugin starts Claude in it with this command:

```bash
claude --dangerously-skip-permissions
```

That means Claude does not ask you before each command. In a yard this is deliberate: nobody sits at a yard's pane to answer questions, and the yard's limits come from HEADING OS's own write guard and the `HEADING_OS_YARD=1` marker, not from those questions.

You can change this with two settings. The plugin runs with the environment the Herdr server started with, not with your current shell, so typing `export ...` before you create a yard does nothing. Put the settings in a file instead:

1. Print the plugin's config folder. Herdr creates it if it does not exist:

   ```bash
   herdr plugin config-dir heading-os.yard
   ```

2. In that folder, create a file named `.env`.
3. Add one or both lines:

   ```
   HEADING_OS_AUTOSTART=0
   HEADING_OS_AGENT_CMD=claude
   ```

| Setting | Default | Effect |
|---|---|---|
| `HEADING_OS_AUTOSTART` | `1` | `1` starts Claude when the yard is ready. `0` leaves the pane empty, and you start Claude yourself. |
| `HEADING_OS_AGENT_CMD` | `claude --dangerously-skip-permissions` | The whole command used to start the assistant. `claude` alone brings the permission questions back. |

The plugin reads only these two names from the file. It never runs the file as a script.

### What the plugin does when a yard is created

You do not have to do any of this by hand. It is listed so that you know what "ready" means and can read a failure.

The plugin writes its progress into `.claude/.yard-bootstrap-status` inside the yard. Each step updates the step number. A ready yard reads `"status":"ok"` and `"step":11`.

1. Finds HELM, the main copy, by asking git.
2. Copies HELM's `.env` into the yard. It removes `WORKSPACE_ROOT` and `HEADING_OS_DATA` from the copy, then writes `HEADING_OS_DATA` pointing at your data repository. That is the value of `HEADING_OS_DATA` in the Herdr server's environment if set, otherwise `.heading-os-data` next to HELM.
3. Turns off pushing from the yard. The yard's push address becomes `DISABLED://use-helm-to-publish`, so nothing can be published from a yard.
4. Installs the Python packages: `uv sync --all-extras --group dev`.
5. Runs `scripts/setup-platform.sh` in the yard, which registers HEADING OS's safety hooks for Claude Code there. Then it installs the data repository write guard and proves it refuses a test write.
6. Confirms the safety hooks are registered. If `codegraph` is on your PATH, it also builds the yard's own code map.
7. Confirms your data repository is outside the yard.
8. Confirms the yard treats itself, not HELM, as its workspace.
9. Runs the clean-tree check, `tests/test_engine_tree_clean.py`, which must pass.
10. Puts a harmless decoy file where private data must never be, and requires the clean-tree check to fail on it. This proves the check is looking at this yard and not somewhere else. Then it removes the decoy.
11. Marks the yard ready, names the workspace in the sidebar after its branch, and starts Claude in the pane with `HEADING_OS_YARD=1` set.

If any step fails, the plugin records the step number with `"status":"failed"`, titles the pane `YARD BROKEN`, shows a notice, and does not start Claude.

## Daily use

The full working method (brief, commit, review, merge) is in `YARDS.md`. Here are the Herdr commands you will use in it.

### Create a yard

From HELM, in the engine folder:

```bash
cd <parent>/.heading-os
herdr worktree create --branch fix-typo-in-readme
```

Choose a short branch name that says what the task is. Herdr then:

- creates the branch from your current `HEAD` if it does not exist yet, or checks it out if it does;
- creates the checkout under your `[worktrees]` folder;
- opens it as a new workspace in the sidebar;
- fires `worktree.created`, and the plugin prepares the yard.

You can also create a yard with the keys: `ctrl+b` then `shift+g` (Herdr's `new_worktree` binding).

The options of `herdr worktree create`, from Herdr's own reference:

| Option | Meaning |
|---|---|
| `--branch <NAME>` | The branch to check out, or to create if it does not exist. |
| `--base <REF>` | What a new branch starts from. Without it, `HEAD`. |
| `--path <PATH>` | Where to put the checkout. Without it, `<worktrees.directory>/<repo>/<branch>`. |
| `--workspace <ID>` or `--cwd <PATH>` | Which repository to branch from: the one open in that workspace, or the one at that folder. Without either, the current one. |
| `--label <TEXT>` | A display name for the new workspace. The plugin renames it after the branch once the yard is ready. |
| `--focus` / `--no-focus` | Switch to the new workspace, or stay where you are. Staying is the default. |
| `--trust-repository` | Tell git to trust a repository owned by another user, for this one command. Use it only after you have checked that repository yourself. It is not a fix to retry with. |

### Wait until the yard is ready

**Do not type anything in the new yard's pane, and do not start Claude there yourself.** The plugin starts Claude as its very last step. If the pane is already busy, the plugin does not start its own Claude, and the yard then runs without its safety marker.

The first time, step 4 (installing Python packages) can take several minutes. Watch the status file:

```bash
cat <yard folder>/.claude/.yard-bootstrap-status
```

When it reads `"status":"ok"` and `"step":11`, the pane title says `YARD ready` and Claude is starting.

### Find a yard's pane id

To send a brief, you need the pane id of the Claude session in the yard. It looks like `w12:p1`: workspace `w12`, pane `p1`. List the running assistants:

```bash
herdr agent list
```

The answer is JSON. Each entry has a `pane_id` and a `cwd` (the folder it runs in). Pick the entry whose `cwd` is your yard folder.

To list workspaces and their ids instead:

```bash
herdr workspace list
```

Each entry has a `workspace_id`, a `label`, and, for a yard, a `worktree.checkout_path`.

### Send the yard its task

Always use the HEADING OS script, from HELM:

```bash
.venv/bin/python scripts/herdr-brief.py w12:p1 "$(cat brief.md)" --no-issue "small fix, no tracker issue"
```

Never send a brief with `herdr agent prompt` by hand. `YARDS.md` explains why, and what the script checks.

### Look at a yard without switching to it

```bash
herdr agent read w12:p1 --lines 40
```

This prints the last 40 lines of that pane's screen. Add `--source visible` to see only what is on screen now.

### Re-check a yard

In the yard's folder:

```bash
HERDR_PLUGIN_EVENT_JSON='{"worktree":{"path":"'"$PWD"'"}}' bash scripts/herdr/heading-os-yard/yard-bootstrap.sh --doctor-only
```

The first part tells the script which folder to check. Without it, the script stops with `the event did not say which worktree to provision` and changes nothing, because it never guesses the folder. It runs the checks of steps 1 and 7 to 11 again without reinstalling anything, and writes the result into the status file. It never starts Claude. The same check is registered in Herdr as the plugin's `doctor` action; `herdr plugin action list --plugin heading-os.yard` shows it.

### Remove a yard

Only after its branch is merged into `main` in HELM (see `YARDS.md`). From HELM:

1. Find the yard's workspace id with `herdr workspace list`. It looks like `w12`.
2. Remove the checkout:

   ```bash
   herdr worktree remove --workspace w12
   ```

   This runs `git worktree remove`, normally closes the yard's workspace too, and fires `worktree.removed`. It never deletes the branch. If git refuses because the yard has uncommitted changes, it stops. `--force` removes the checkout anyway and throws those changes away, so look at them first.
3. If the workspace is still in the sidebar afterwards, close it: `herdr workspace close w12`.
4. Delete the branch: `git branch -d fix-typo-in-readme`. Git refuses this while a worktree still uses the branch, and also when the branch is not merged.

`--workspace` takes the workspace id. It never takes a branch name or a folder path.

## Check that it works

Run these from HELM after setup. Each line says what you should see.

1. `herdr --version` prints `0.8.0` or later.
2. `herdr status` shows a `client:` and a `server:` block, and the server says `status: running`.
3. `herdr config check` prints `config: ok`.
4. `herdr integration status` shows `claude: current`.
5. `herdr plugin list` shows `heading-os.yard` as `enabled`.
6. `herdr plugin list --plugin heading-os.yard --json` lists the three events and the `doctor` action.
7. `.venv/bin/python scripts/install-data-overlay-guard.py --check` reports the hook's state and changes nothing.
8. Create a test yard: `herdr worktree create --branch herdr-check`. Wait, then read its `.claude/.yard-bootstrap-status`. It must reach `"status":"ok"` and `"step":11`, and Claude must start in the pane.
9. Read the plugin's log: `herdr plugin log list --plugin heading-os.yard --limit 5`.
10. Check a brief without sending it: `.venv/bin/python scripts/herdr-brief.py w12:p1 "$(cat brief.md)" --check`. It prints `Brief text accepted. Nothing was sent (--check).`
11. Remove the test yard as shown above, then delete its branch with `git branch -D herdr-check`. The capital `-D` is needed because this test branch was never merged.

## Common problems

**`herdr: command not found`.** The folder Herdr was installed into is not on your PATH. Run the PATH step of the install section again, then open a new terminal.

**The install script stops with `requires 'curl'` or `requires 'awk'`.** Install that tool first. On Ubuntu: `sudo apt install -y curl gawk`.

**The install script stops with `unsupported OS` or `unsupported architecture`.** Herdr's install script supports Linux and macOS on x86_64 and arm64 only. On Windows, run it inside Ubuntu (WSL2).

**A new yard has no `.claude/.yard-bootstrap-status` at all.** The plugin did not run. Check, in this order:

1. `herdr plugin list` shows `heading-os.yard` as `enabled`. If it says disabled, run `herdr plugin enable heading-os.yard`.
2. `herdr plugin list --plugin heading-os.yard --json` lists `worktree.created`. If not, unlink and link the plugin again.
3. `herdr plugin log list --plugin heading-os.yard` shows a line starting `[YARD] STOP: the event did not say which worktree to provision`. Then the plugin could not find the yard's folder in Herdr's event, and it changed nothing. Run it by hand, from inside the yard:

   ```bash
   FORCE_BOOTSTRAP=1 HERDR_PLUGIN_EVENT_JSON='{"worktree":{"path":"'"$PWD"'"}}' bash scripts/herdr/heading-os-yard/yard-bootstrap.sh
   ```

**The status file says `"status":"failed"`.** Read its `step` number to see where it stopped. The plugin log says why, in a line that starts `[YARD] STOP at step`.

| Step | Usual cause | Fix |
|---|---|---|
| 1 | The folder is not a git checkout. | Create the yard with `herdr worktree create`. |
| 2 | No data repository at the expected place. | Keep `.heading-os-data` next to `.heading-os`, or start the Herdr server with `HEADING_OS_DATA` set. |
| 4 | `uv is not on PATH` for the Herdr server, or `uv sync` failed. | Make sure `uv` is on PATH in the shell you start Herdr from. Restart the server from that shell (see the next problem). |
| 5 | `setup-platform.sh` failed, or the data write guard did not refuse its test write. | Run `bash scripts/setup-platform.sh` in the yard and read its message. |
| 6 | The safety hooks are not registered. | Run `bash scripts/setup-platform.sh --check` in the yard. |
| 7 or 8 | A `WORKSPACE_ROOT` or a wrong `HEADING_OS_DATA` reached the yard. | Remove `WORKSPACE_ROOT` from HELM's `.env`, then re-run the plugin by hand with `FORCE_BOOTSTRAP=1` as shown above. |
| 9 | The clean-tree check fails on the yard. | Read `.claude/.yard-bootstrap-tree-clean.log` in the yard. |
| 10 | The clean-tree check did not see the decoy. | Read `.claude/.yard-bootstrap-canary.log`. The check is looking at the wrong folder. Do not work in this yard until it passes. |

**Step 4 says `uv is not on PATH`, but `uv` works in your terminal.** The plugin uses the environment of the Herdr server, which was fixed when the server started. Restarting the server closes every pane and everything running in them, so save your work first. Then run `herdr server stop`, open a terminal where `uv --version` works, and start `herdr` again from there.

**No notices appear.** `ui.toast.delivery` is still `off`. Set it to `"herdr"` and run `herdr server reload-config`. The plugin log records each notice that was not shown, with the reason.

**The pane title says `YARD ready, agent NOT started`.** The plugin found something already running in the pane, so it did not type its start command into it. The log names what it found and prints the exact command to use. If no Claude with the yard marker is running there, stop what is there and start one:

```bash
herdr pane run w12:p1 "HEADING_OS_YARD=1 claude --dangerously-skip-permissions"
```

**Claude in a yard shows `YARD MARKER MISSING` at start.** That session was started without `HEADING_OS_YARD=1`, so some yard safety checks cannot see it. Close that Claude session, then in the same pane run `HEADING_OS_YARD=1 claude --dangerously-skip-permissions`.

**You closed Claude in a yard by accident.** The pane keeps its shell and prints two commands. `claude --dangerously-skip-permissions --continue` brings back the same conversation. The shell still has `HEADING_OS_YARD=1` set, so either command keeps the yard marker.

**`herdr worktree remove` answers with an error about the workspace.** You passed a branch name or a path. Pass the workspace id from `herdr workspace list`.

**Herdr's own logs.** They sit next to the config file: `herdr.log`, `herdr-client.log` and `herdr-server.log`. `herdr --help` prints their folder on its `Logs:` line. For more detail, start Herdr with `HERDR_LOG=herdr=debug`.

## Updating Herdr

1. If you installed with the install script, update with:

   ```bash
   herdr update
   ```

   It downloads the newest version from your update channel. `herdr channel show` prints the channel; the default is `stable`.
   If you installed with Homebrew, mise or Nix, update through that tool instead.
2. Check both halves: `herdr status`. The `client:` version is the program you just installed. The `server:` version is the one still running in the background. They can differ until the server restarts. Restarting the server closes all panes, so do it when no yard is working.
3. After an update, check the plugin is still registered with all three events: `herdr plugin list --plugin heading-os.yard --json`.

Herdr's API version can change between releases. `herdr api schema --json` prints the current `protocol` and `schema_version` numbers if you need them for a problem report.

## Removing Herdr

Herdr does not publish uninstall steps. These steps undo what this guide did.

1. Merge or abandon every open yard, and remove each one with `herdr worktree remove --workspace <id>`.
2. Unlink the plugin: `herdr plugin unlink heading-os.yard`. This leaves the plugin's files in your engine folder.
3. Remove the Claude Code hook: `herdr integration uninstall claude`.
4. Stop the server: `herdr server stop`. This closes every pane and everything running in them.
5. Delete the program. For the install script, that is `rm ~/.local/bin/herdr` (or the folder you set in `HERDR_INSTALL_DIR`). For Homebrew, `brew uninstall herdr`.
6. If you want no trace left, delete Herdr's config folder `~/.config/herdr` and, if it exists and holds no yard you still need, `~/.herdr`.

The data repository guard from "Connect Herdr to HEADING OS" stays useful without Herdr, so you can keep it.

## What is not covered here

- Herdr features HEADING OS does not use: themes, remote machines (`herdr machine`, `--remote`), named sessions and Herdr's own plugin catalogue. See https://herdr.dev.
- The macOS steps come from Herdr's install script and documentation and from the plugin's source. They were not run on a Mac for this guide.
- On macOS, the plugin's check for "is something already running in this pane" reads `/proc`, which macOS does not have. Only the part that asks Herdr about the pane runs there. Wait for the yard to be ready before you touch its pane.
