# AgentView: see what your agents do and what they use

AgentView is the name this guide gives to three small tools. Each one shows you a different part of the work your AI assistants do. All three are optional. HEADING OS runs fine without any of them.

| Tool | What it shows you | Where it comes from |
|---|---|---|
| `agent-watch` | The helper agents (subagents) of the Claude Code session you are in right now: which model each one uses, how far it got, how long it has run, and a cost estimate. | Ships inside the engine, in `scripts/claude-mods/agent-watch/`. |
| Proxy usage dashboard | How many tokens every call to another AI model used (ChatGPT, Kimi, Gemini, Grok), by day, by model and by the script that made the call. | Ships inside the engine: `scripts/proxy-usage.py`. |
| AgentsView | A searchable archive of all your past coding-agent sessions, with token and cost reports. | A separate free program from another author (https://www.agentsview.io, MIT license). |

A few words you will see below:

- **Claude Code** is the program you talk to Claude in. HEADING OS runs inside it.
- **Subagent** means a helper agent that Claude starts to do one part of a job while you wait.
- **Token** means a small piece of text. AI models count their work in tokens, and providers price it in tokens.
- **The engine** is the HEADING OS code folder, `<parent>/.heading-os`. **The data repository** is your private folder beside it, `<parent>/.heading-os-data`. The default `<parent>` is `~/ai/claude-workspaces`. See `INSTALL.md`.
- **CLIProxyAPI** is the small local program that carries every call to a non-Claude model. It has its own guide, `CLIPROXY.md`.

Run every command in this guide from the engine folder unless a step says otherwise:

```bash
cd <parent>/.heading-os
```

---

## Part 1. agent-watch: the subagent panel

### What it is and why HEADING OS uses it

Claude often hands work to subagents. Without help you cannot see them: you only see that Claude is busy. `agent-watch` fixes that. It adds two things to Claude Code:

1. **A band above the prompt.** It appears while subagents run and stays for 30 seconds after the last one ends.
2. **A panel named "Agents".** At the top, three boxes show the session's estimated cost, its tokens, and how long the current turn has run. Below them, the subagents of this session are listed in two groups, "Running" and "Finished". Each one shows its description, its type and model, its time and cost, and a progress bar. The last line splits the total between the subagents and the main conversation.

It also gives subagents a small tool named `report_step`. A subagent can call it with `done` (steps finished), `total` (steps planned) and `note` (a few words about the current step). The panel then fills that agent's progress bar.

What it does **not** do. It reads only what Claude Code hands to it. It makes no network call and no model call. It reads and writes no file and starts no process. So it cannot leak anything and it cannot slow a session down.

About the cost figure. It is an estimate in US dollars, computed from a price table inside the plugin (`hooks/register.tsx`). That table was copied from the Anthropic pricing page. A model that is not in the table is shown as "no price", never as zero. A total with a `+` at the end, such as `≈$0.42+`, means some requests had no price. It counts only from the moment the plugin loaded in this session. If you use Claude through a subscription, you do not pay this amount. It only tells you how heavy the work was.

### What you need first

- Claude Code installed and working. Check with `claude --version`. This guide was checked with Claude Code 2.1.310.
- The HEADING OS engine cloned to `<parent>/.heading-os` (see `INSTALL.md`).

Not documented yet: the oldest Claude Code version that can load this plugin. The plugin uses Claude Code's newer "mod" interface (a code module named in `hooks/hooks.json`). If an older Claude Code refuses it, update Claude Code first.

### Install, step by step (Linux, WSL and macOS are the same)

The engine contains a small local plugin catalogue (Claude Code calls it a "marketplace") named `heading-os-mods`. It lives in `scripts/claude-mods/`. You register that folder once, then install the plugin from it.

1. Check that the catalogue is valid:

   ```bash
   claude plugin validate scripts/claude-mods
   ```

   You should see `✔ Validation passed`.

2. Register the catalogue. Give the full path, because Claude Code remembers it:

   ```bash
   claude plugin marketplace add <parent>/.heading-os/scripts/claude-mods
   ```

3. Install the plugin:

   ```bash
   claude plugin install agent-watch@heading-os-mods
   ```

   By default this installs it for your user, in every project. To limit it, add `--scope project` or `--scope local`.

4. Close Claude Code and start it again. Plugins load when a session starts.

### First-time setup

There is nothing to configure. The plugin has no settings, no keys and no files.

### How it connects to HEADING OS

Claude Code reads the plugin straight from your engine folder. `claude plugin list` shows the line `Read from: <parent>/.heading-os/scripts/claude-mods/agent-watch`. So when you update the engine with `git pull`, the plugin is updated too. Restart Claude Code to load the new version.

The engine has no setting for it in `.env`. The plugin and the engine do not talk to each other.

### Daily use

- **Open or close the panel.** Type `/agent-watch` in a Claude Code session. Claude Code answers `Agent panel opened.` or `Agent panel closed.`
- **Watch a job.** Ask Claude for something big, for example "review every file in this folder for spelling mistakes, using subagents". While the subagents run, the band above the prompt shows them. The panel shows one row for each.
- **Read a row.** First line: what the agent is doing, and a mark for its state. Second line: its type and its model. Third line: time and cost. Fourth line: the progress bar.
- **Finished agents.** They move to the "Finished" group, which also counts failed ones. Select the group's title to fold or unfold it.

### How to check it works

1. Run `claude plugin list`. Find `agent-watch@heading-os-mods`. Its status must read `✔ enabled`.
2. Start a new session and type `/agent-watch`. You must see `Agent panel opened.` and a panel titled "Agents".
3. Ask Claude for any task that starts a subagent. A row appears while it runs.

### Common problems and fixes

| What you see | Why, and what to do |
|---|---|
| `/agent-watch` is an unknown command | The session started before the install. Restart Claude Code. If it still fails, run `claude plugin list` and check the plugin is enabled. If it is disabled, run `claude plugin enable agent-watch@heading-os-mods`. |
| `claude plugin install` says the plugin is not found | The catalogue is not registered. Run step 2 again, then check with `claude plugin marketplace list`. You must see `heading-os-mods`. |
| The plugin disappeared after you moved the engine folder | Claude Code still points at the old path. Remove the catalogue (see "Removing" below) and add it again with the new path. |
| The cost shows `no price` | The model is not in the plugin's price table. The token counts are still right. |
| The panel says `No subagents in this session yet.` | No subagent has run in this session yet. The panel only lists this session's subagents. |

### Updating and removing

- **Update.** Update the engine (`git pull` in `<parent>/.heading-os`), then run `claude plugin marketplace update heading-os-mods` so Claude Code re-reads the catalogue. Restart Claude Code.
- **Turn it off for a while.** `claude plugin disable agent-watch@heading-os-mods`. Turn it on again with `claude plugin enable agent-watch@heading-os-mods`.
- **Remove it.** Run `claude plugin uninstall agent-watch@heading-os-mods`. To forget the catalogue too, run `claude plugin marketplace remove heading-os-mods`. Neither command touches the files in your engine folder.

---

## Part 2. The proxy usage dashboard

### What it is and why HEADING OS uses it

HEADING OS sends some work to models other than Claude: ChatGPT, Kimi, Gemini and Grok. Every such call goes through one function in the engine, `call_model` in `scripts/utils/proxy_transport.py`, and from there through CLIProxyAPI (see `CLIPROXY.md`). CLIProxyAPI itself keeps no per-call token counts. So the engine keeps them.

After every request, `call_model` adds one line to a log file in your data repository. That file is called the **usage ledger**. Each line records:

- the time (UTC),
- the model the script asked for, and the model the proxy actually used,
- the script that made the call (for example `kimi-consult.py`),
- the token counts: input, cached input, output, reasoning, total,
- the HTTP status of a failed reply, how long the request took, and how it ended (`ok`, `empty`, `length`, `content_filter` or `error:<name>`).

A line **never** holds the question or the answer. It says that a call happened and how many tokens it used, nothing about what was said.

A retry is a request too, so it gets its own line. If writing the line fails, the model call still succeeds and you see one warning that starts with `[proxy-usage] WARNING`.

`scripts/proxy-usage.py` reads the ledger and shows it two ways: as a table in the terminal, and as one web page you open in a browser.

Beside the tokens it shows a dollar figure titled "$ API LIST". That is what the same tokens would cost at each provider's public API price list. It is a reference figure, **not your bill**: the proxy usually runs on flat monthly subscriptions. The price list comes from the public `pydantic/genai-prices` table on GitHub. The script downloads a fresh copy at most once a day and keeps the last good copy if a download fails.

What it covers. Only the calls the engine makes through `call_model`. Calls that other programs send straight to the proxy are not in the ledger.

### What you need first

- The engine installed, with its Python environment (`.venv`), and your data repository beside it (see `INSTALL.md`).
- CLIProxyAPI working, if you want any data to show (see `CLIPROXY.md`). Without it the report is simply empty.
- For the automatic refresh: Linux or WSL2 with systemd. On macOS there is no automatic refresh (see below).

### Install

Nothing to install. The script and the ledger code ship with the engine.

### First-time setup

1. **Set your time zone.** The report groups calls by day in your local time. Open `<parent>/.heading-os/.env` and set `HEADING_OS_TZ` to your IANA time zone name, for example:

   ```bash
   HEADING_OS_TZ=Europe/Berlin
   ```

   Without it the days follow UTC. You can also pass `--timezone <zone>` to any report.

2. **Optional: teach it your prices.** The proxy calls models by its own names, such as `kimi-for-coding` or `k3`. Those names match no row in the price list, so every call is "unpriced" until you map them. The engine ships no mapping on purpose. You write it once, in your data repository, at `system/config/proxy-usage-price-aliases.yaml`. Print its full path with:

   ```bash
   .venv/bin/python scripts/data-path.py path proxy-usage-price-aliases
   ```

   The file looks like this. Each key is a model name you send to the proxy. Each value names one row of the price list, or `null` to leave the model unpriced on purpose:

   ```yaml
   aliases:
     <proxy-model-name>: {provider: <provider-id>, model: <model-id>}
     <another-proxy-model>: null
   ```

   To find the right `<provider-id>` and `<model-id>`, run any report once (so the price list is downloaded), then list the rows and search them. This example searches for Kimi:

   ```bash
   .venv/bin/python -c "import json,sys; [print(p['id'], m['id']) for p in json.load(open(sys.argv[1])) for m in p['models']]" "$(.venv/bin/python scripts/data-path.py path proxy-usage-prices)" | grep -i kimi
   ```

   Each output line is `<provider-id> <model-id>`. Prefer the model maker's own row over a reseller's. A name you do not list stays unpriced, and the report says how many calls were unpriced.

3. **Optional: a different price list.** Set `PROXY_USAGE_PRICES_URL` in `.env`, or pass `--prices-url <https-url>`. Most people never need this.

### How it connects to HEADING OS

| Piece | Where it is |
|---|---|
| The code that writes the ledger | `scripts/utils/proxy_usage.py`, called by `call_model` in `scripts/utils/proxy_transport.py` |
| The ledger | `system/scratch/proxy-usage/usage.jsonl` in your data repository (registry name `proxy-usage-ledger`) |
| The web page | `system/reports/proxy-usage/proxy-usage.html` in your data repository (registry name `proxy-usage-reports`) |
| The downloaded price list | `system/scratch/proxy-usage/genai-prices.json` (registry name `proxy-usage-prices`) |
| Your price mapping | `system/config/proxy-usage-price-aliases.yaml` (registry name `proxy-usage-price-aliases`) |
| Settings read from `.env` | `HEADING_OS_TZ`, `PROXY_USAGE_PRICES_URL` (both optional) |
| The refresh timer | `proxy-usage-dashboard.timer`, installed by `scripts/install-proxy-usage-timer.sh` |

To print the real path of any row in the table, run `.venv/bin/python scripts/data-path.py path <registry-name>`.

### Daily use, with examples

The report command is `daily`. With no options it shows the last 30 days, one row per day.

```bash
.venv/bin/python scripts/proxy-usage.py daily
```

You get columns `DATE`, `CALLS`, `RETRIES`, `ERRORS`, `INPUT`, `OUTPUT`, `REASONING`, `TOTAL`, `$ API LIST` and `MODELS`, then a `TOTAL` row and two grey notes: where the prices came from, and how many calls were unpriced.

More examples:

| You want | Command |
|---|---|
| One sub-row per model under each day | `.venv/bin/python scripts/proxy-usage.py daily --breakdown` |
| One sub-row per calling script | `.venv/bin/python scripts/proxy-usage.py daily --by caller` |
| The last 7 days | `.venv/bin/python scripts/proxy-usage.py daily --since 7d` |
| One month | `.venv/bin/python scripts/proxy-usage.py daily --since 2026-01-01 --until 2026-01-31` |
| Everything, as JSON for another program | `.venv/bin/python scripts/proxy-usage.py daily --all --json` |
| Days in another time zone | `.venv/bin/python scripts/proxy-usage.py daily --timezone America/New_York` |
| Fetch the price list now | `.venv/bin/python scripts/proxy-usage.py daily --refresh-prices` |
| Write the web page and print where it is | `.venv/bin/python scripts/proxy-usage.py daily --html` |
| Rebuild the web page only if something changed | `.venv/bin/python scripts/proxy-usage.py dashboard` |
| Rebuild the web page no matter what | `.venv/bin/python scripts/proxy-usage.py dashboard --force` |

`--json` and `--html` cannot be used together. `--since` and `--until` take a number of days such as `28d`, or a date as `YYYY-MM-DD`.

**The web page.** It is one self-contained file. It loads nothing from the internet, so you can open it offline. It reloads itself every 5 minutes. At the top you can switch between Cost and Tokens, pick a chart style (Smooth, Lines, Bars), group by model, caller or provider, pick 7, 30 or 90 days or all, and filter by model or caller. Your choices are kept in the address after the `#` sign, so a reload keeps them. For example: `proxy-usage.html#mode=cost&chart=bars&group=caller&period=7`.

To open it:

- **Linux:** `xdg-open "$(.venv/bin/python scripts/data-path.py path proxy-usage-reports)/proxy-usage.html"`
- **macOS:** `open "$(.venv/bin/python scripts/data-path.py path proxy-usage-reports)/proxy-usage.html"`
- **WSL2:** print the Windows form of the path with `wslpath -w "$(.venv/bin/python scripts/data-path.py path proxy-usage-reports)/proxy-usage.html"`, then paste it into the address bar of your Windows browser.

### Keep the web page fresh automatically (Linux and WSL2)

The engine ships a timer that rebuilds the page every 5 minutes, but only when the ledger changed. When nothing changed, the run reads one small piece of the page and stops.

1. Make sure you are in the **main** engine folder, `<parent>/.heading-os`. The installer refuses to run from a git worktree (a "yard", see `YARDS.md`), because the timer would point at a folder that is deleted later.
2. Make sure systemd runs. On WSL2, open `/etc/wsl.conf` and add:

   ```ini
   [boot]
   systemd=true
   ```

   Then restart WSL so the change takes effect, and open your Linux terminal again.
3. Run the installer:

   ```bash
   bash scripts/install-proxy-usage-timer.sh
   ```

   It writes two files to `~/.config/systemd/user/`: `proxy-usage-dashboard.service` and `proxy-usage-dashboard.timer`. It enables the timer, and it turns on "linger" for your user (`loginctl enable-linger`) so the timer also runs after a reboot when nobody has logged in. It takes the time zone from `HEADING_OS_TZ`. To force a zone just for the install, run `HEADING_OS_TZ=Europe/Berlin bash scripts/install-proxy-usage-timer.sh`.
4. The installer ends by printing the next run time and three useful commands.

**macOS:** the installer needs `systemctl`, which macOS does not have, and the engine ships no macOS scheduler for this page. Run `.venv/bin/python scripts/proxy-usage.py dashboard` yourself when you want a fresh page.

### How to check it works

1. Make one call through the proxy. The simplest is a harmless question to one model (see `CLIPROXY.md`, "How to check it works"):

   ```bash
   .venv/bin/python scripts/kimi-consult.py --mode independent --question "Reply with the single word ready."
   ```

2. Run `.venv/bin/python scripts/proxy-usage.py daily --since 1d`. Today's row must show at least one call.
3. Run `.venv/bin/python scripts/proxy-usage.py dashboard --force`, then open the page. Today must appear in the chart.
4. If you installed the timer: `systemctl --user status proxy-usage-dashboard.timer` must show `active (waiting)`, and `systemctl --user list-timers proxy-usage-dashboard.timer` shows the next run.

### Common problems and fixes

| What you see | Why, and what to do |
|---|---|
| The report is empty | No call went through `call_model` yet, or CLIProxyAPI is not set up. Make one call (see above). |
| `[proxy-usage] WARNING: usage row not recorded` after a model call | The ledger could not be written, often because the data repository is missing or not found. The model answer is still fine. Check that `<parent>/.heading-os-data` exists, then run `.venv/bin/python scripts/data-path.py path proxy-usage-ledger`. |
| `$ API LIST` is `$0.00` and the note says most calls are unpriced | You have no price mapping yet. See "First-time setup", step 2. |
| A yellow note says the price list was NOT refreshed | The download failed (no internet, or GitHub refused). The report uses the last good copy. Try again later or pass `--refresh-prices`. |
| `unknown timezone: '...'` | The name in `HEADING_OS_TZ` or `--timezone` is not a valid IANA name. Use a name like `Europe/Berlin`. |
| The days look shifted by some hours | `HEADING_OS_TZ` is not set, so days follow UTC. Set it (see "First-time setup"). |
| The installer prints `systemctl not found` and exits | systemd is off. On WSL2, turn it on (see step 2 above). |
| The installer refuses and talks about a worktree or the main clone | You ran it from a yard. Run it from `<parent>/.heading-os`. |
| The installer says `this systemd rejects the calendar expression` | Your systemd does not accept a time zone in a timer. Follow the hint it prints: remove the time zone at the end of the `OnCalendar=` line in `~/.config/systemd/user/proxy-usage-dashboard.timer`, set your machine's time zone instead, and run the installer again. |
| The page does not change | The timer is not running, or nothing new was logged. Check with `journalctl --user -u proxy-usage-dashboard.service -n 20`. |

### Updating and removing

- **Update.** The script is part of the engine. `git pull` in the engine updates it. The timer needs no reinstall, because it runs the script from the engine folder.
- **Remove the timer.** `bash scripts/install-proxy-usage-timer.sh --uninstall`. It stops the timer and deletes its two files. The ledger and the page stay where they are.
- **Stop logging.** There is no switch. The ledger is part of `call_model`. If you do not want the records, do not use the proxy.

---

## Part 3. AgentsView: the archive of past sessions

### What it is and why HEADING OS uses it

AgentsView "brings your AI coding sessions into one searchable archive" (source: https://www.agentsview.io). It reads the transcript files that Claude Code and many other coding agents leave on your disk. It copies them into a local SQLite database (one file, no server database). Then it gives you a web page to browse and search them, and terminal reports of tokens and cost. Its source code is at https://github.com/kenn-io/agentsview.

HEADING OS does not call AgentsView and does not need it. It is recommended because it answers a question the engine does not: "what did my Claude sessions cost and do over the last month?" The proxy usage dashboard above was built to read like AgentsView's daily report on purpose. So the two cover different calls in the same layout:

- AgentsView: your coding-agent sessions (Claude Code and others).
- Proxy usage dashboard: the calls the engine sends to other models.

### What you need first

- macOS or Linux (WSL2 counts as Linux). The installer refuses other systems.
- `curl` and `bash`.

### Install, step by step

1. Run the official installer:

   ```bash
   curl -fsSL https://agentsview.io/install.sh | bash
   ```

   It downloads the right build for your system. It installs into `/usr/local/bin` when you can write there, and into `~/.local/bin` otherwise.
2. Open a new terminal, then check: `agentsview --version`. This guide was checked with `agentsview v0.46.1`.
3. If the shell says `agentsview: command not found`, `~/.local/bin` is not on your `PATH`. Add the line `export PATH="$HOME/.local/bin:$PATH"` to `~/.bashrc` (Linux and WSL) or `~/.zshrc` (macOS) and open a new terminal.

Other ways to install (Windows, desktop app, pip, Docker) are listed in the upstream quick start: https://agentsview.io/docs/quickstart/.

### First-time setup

1. Start the background server: `agentsview daemon start`.
2. Load your sessions once: `agentsview sync`. It reads your agents' session files into its database. It starts the server first if it is not running.
3. On the test machine for this guide, AgentsView kept its data in the folder `~/.agentsview`.

### How it connects to HEADING OS

It does not. Nothing in the engine reads from or writes to AgentsView, and no `.env` setting refers to it. The only link is the shared layout of the daily report.

### Daily use, with examples

| You want | Command |
|---|---|
| Is the server running, and where? | `agentsview serve status` |
| Cost per day for the last 30 days | `agentsview usage daily` |
| With one row per model | `agentsview usage daily --breakdown` |
| The last 7 days | `agentsview usage daily --since 7d` |
| Only Claude Code sessions | `agentsview usage daily --agent claude` |
| As JSON | `agentsview usage daily --json` |
| Days in your time zone | `agentsview usage daily --timezone Europe/Berlin` |
| Search past sessions for a word | `agentsview session search <word>` |
| List your projects | `agentsview projects` |

To browse, open `http://127.0.0.1:8080` in a browser. That is the default address: `agentsview serve --help` lists `--host` with a default of `127.0.0.1` (only your own machine can reach it) and `--port` with a default of `8080`. To use another port, run the server yourself with `agentsview serve --port <number>`.

### How to check it works

1. `agentsview serve status` must print `agentsview running at http://127.0.0.1:8080` (or your port) with a version and an uptime.
2. `agentsview usage daily --since 7d` must print a table with rows for days you used Claude Code.
3. The web page at that address must list your recent sessions.

### Common problems and fixes

| What you see | Why, and what to do |
|---|---|
| `agentsview: command not found` | See install step 3. |
| `serve status` says nothing is running | Run `agentsview daemon start`. |
| The page does not load | The server is not running, or it uses another port. `agentsview serve status` prints the real address. |
| A recent session is missing | It has not been synced yet. Run `agentsview sync`. |
| Port 8080 is taken by another program | Stop the daemon with `agentsview daemon stop`, then run `agentsview serve --port <free-port>`. |

### Updating and removing

- **Check for an update:** `agentsview update --check`.
- **Install the update:** `agentsview update`. Add `--yes` to skip the question.
- **Stop it:** `agentsview daemon stop`.
- **Remove it:** stop it first, then delete the program file (`/usr/local/bin/agentsview` or `~/.local/bin/agentsview`; `command -v agentsview` tells you which). Its archive lives in `~/.agentsview`; delete that folder only if you no longer want your session history.

Not documented yet: whether `agentsview daemon start` sets up an automatic start after a reboot on every system. If the server is gone after a reboot, run `agentsview daemon start` again.
