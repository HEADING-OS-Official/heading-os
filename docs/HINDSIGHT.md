# Hindsight: the memory server

This guide takes you from nothing to daily use of Hindsight with HEADING OS. It assumes you have never seen Hindsight before. Hindsight is optional. HEADING OS runs without it, and every part that uses it stays silent until you finish section 5.1.

Every command, flag, file name, port and default here was checked against the Hindsight CLI 1.1.0 (its own `--help`), Hindsight's upstream documentation for version 1.1, the README of the coding-agents plugin 0.8.2, or the HEADING OS engine source. Where something could not be checked, the guide says so.

## Contents

1. [What Hindsight is, and why HEADING OS uses it](#1-what-hindsight-is-and-why-heading-os-uses-it)
2. [What you need first](#2-what-you-need-first)
3. [Install and run the server](#3-install-and-run-the-server)
4. [First-time setup: the CLI and the bank](#4-first-time-setup-the-cli-and-the-bank)
5. [Connect it to the engine](#5-connect-it-to-the-engine)
6. [What changes in your sessions](#6-what-changes-in-your-sessions)
7. [Daily use, with worked examples](#7-daily-use-with-worked-examples)
8. [Check that it works](#8-check-that-it-works)
9. [Common problems and fixes](#9-common-problems-and-fixes)
10. [Update and remove](#10-update-and-remove)
11. [What this guide does not cover yet](#11-what-this-guide-does-not-cover-yet)

## 1. What Hindsight is, and why HEADING OS uses it

Hindsight is a memory server made by Vectorize (source: `https://github.com/vectorize-io/hindsight`). You give it text. It reads the text with an AI model, pulls out the facts, and stores them. Later you ask it a question in plain words, and it gives back the facts that answer it, each with the document it came from.

A few words you will meet:

- **Server.** The Hindsight program that runs all the time and stores everything. It answers on a port, by default `8888`. A port is a numbered door on your computer that programs use to talk to each other.
- **Bank.** One separate memory inside the server. HEADING OS uses one bank, named `main`.
- **Document.** One piece of text you gave the bank, with an id. HEADING OS gives each file the id of its path in your data repository.
- **Retain, recall, reflect.** Retain means "store this". Recall means "find the facts about this". Reflect means "think over what you hold and answer this".
- **API key.** A long password that programs send to the server to prove they may use it.
- **MCP.** Model Context Protocol, the way Claude Code talks to outside tools. Through it, Claude can ask Hindsight questions in a session.

**Why HEADING OS uses it.** Claude forgets everything when a session ends. Your data repository remembers, but only if someone knows which file to open. Hindsight lets every session ask "what do we know about this?" in plain words and get an answer with its source. With Hindsight connected, HEADING OS:

- **asks Hindsight about every message you type** and shows Claude the facts it found, before Claude answers;
- **copies the text of your data repository into the bank** and keeps it current, so a changed file replaces its old copy and a deleted file's copy is removed;
- **keeps every session** in the bank as a conversation, through Hindsight's own Claude Code plugin;
- **takes a daily snapshot** to disk of what lives only in Hindsight, and answers from it when the server is down.

> **Warning:** the sync sends the text of your whole data repository to the Hindsight server, and the server sends that text to the AI model you choose for fact extraction. Choose a server location and a model provider you trust with all of your data.

## 2. What you need first

- **HEADING OS installed**, following [INSTALL.md](INSTALL.md). This guide calls the parent folder `<parent>`. The engine is `<parent>/.heading-os` and your data repository is `<parent>/.heading-os-data`.
- **Claude Code** working (`claude --version` prints a version).
- **Docker**, to run the server the simple way (`docker --version` prints a version). On Windows, use Docker inside WSL2 or Docker Desktop with WSL2. Without Docker you can use Python instead (section 3.3).
- **Node.js with `npx`** for the Claude Code plugin (`npx --version` prints a version). If you followed INSTALL.md, you already have it.
- **An account with an AI model provider and its API key**, for fact extraction. Upstream lists more than 25 providers; the default is OpenAI.
- **Memory on the machine that runs the server.** Upstream gives a minimum of 1.5 GB of RAM for the full server, and 512 MB more for its database.

The server can run on the same computer as HEADING OS, or on another one you reach over SSH (section 3.4).

## 3. Install and run the server

### 3.1 Choose an API key for the server

Make up a long random key now. This command prints one:

```bash
openssl rand -hex 32
```

Copy it somewhere safe for the next steps. This guide calls it `<your-key>`. Treat it like a password.

Hindsight 1.1 ships with authentication off. Turn it on. Upstream's built-in way is two settings: `HINDSIGHT_API_TENANT_EXTENSION=hindsight_api.extensions.builtin.tenant:ApiKeyTenantExtension` and `HINDSIGHT_API_TENANT_API_KEY=<your-key>`. Every request must then carry `Authorization: Bearer <your-key>`, and a request without it is refused.

### 3.2 Linux, WSL2 or macOS: Docker (recommended)

This is upstream's one-container command, with the two authentication settings added. It runs the server and its own built-in database together. Replace `sk-xxx` with your OpenAI key and `<your-key>` with the key from 3.1:

```bash
export OPENAI_API_KEY=sk-xxx
HS_USER=hindsight   # the user the container runs as; its data folder is that user's .pg0

docker run -it --pull always --name hindsight --restart unless-stopped --shm-size=1g -p 8888:8888 -p 9999:9999 \
  -e HINDSIGHT_API_LLM_API_KEY=$OPENAI_API_KEY \
  -e HINDSIGHT_API_TENANT_EXTENSION=hindsight_api.extensions.builtin.tenant:ApiKeyTenantExtension \
  -e HINDSIGHT_API_TENANT_API_KEY=<your-key> \
  -v hindsight-data:/home/$HS_USER/.pg0 \
  ghcr.io/vectorize-io/hindsight:latest
```

What each part means:

- `-p 8888:8888` opens the API on port 8888. `-p 9999:9999` opens the web page (upstream calls it the Control Plane) on port 9999.
- `-v hindsight-data:/home/$HS_USER/.pg0` keeps the data in a Docker volume called `hindsight-data`, so it survives restarts and updates. Inside the container, Hindsight keeps it in an embedded PostgreSQL database; you never manage it yourself.
- `--restart unless-stopped` starts it again after a reboot.
- `-it` keeps it attached to your terminal so you can watch it start. To leave it running in the background instead, use `-d` in place of `-it`.

**Another AI provider.** Add `-e HINDSIGHT_API_LLM_PROVIDER=<provider>` and, where needed, `-e HINDSIGHT_API_LLM_MODEL=<model>`. The provider names include `openai` (the default, with model `gpt-5-mini`), `anthropic`, `gemini`, `groq`, `ollama` and `lmstudio`; the full list is on upstream's configuration page (`https://hindsight.vectorize.io/developer/configuration`).

When it has started:

- the API answers at `http://localhost:8888`;
- the web page is at `http://localhost:9999`.

### 3.3 The pip route, without containers

```bash
pip install hindsight-api
export HINDSIGHT_API_LLM_API_KEY=sk-xxx
export HINDSIGHT_API_TENANT_EXTENSION=hindsight_api.extensions.builtin.tenant:ApiKeyTenantExtension
export HINDSIGHT_API_TENANT_API_KEY=<your-key>
hindsight-api
```

Upstream says this creates its database in `~/.hindsight/data/` and starts the API on `http://localhost:8888`. Useful options: `--port 9000` for another port, `--host 127.0.0.1` to answer only this computer. It runs while the terminal stays open. On an Intel Mac, upstream says to use `hindsight-api-slim`, which needs outside services for its models; Docker is easier there.

### 3.4 The server on another machine

You can run the server on a separate machine, for example a small home server, and reach it from HEADING OS through an SSH tunnel. A tunnel makes the remote port look like a port on your own computer, so every default in this guide (`127.0.0.1:8888`) keeps working:

```bash
ssh -N -L 8888:127.0.0.1:8888 <user>@<server>
```

`-N` means "only forward the port, run no command". Keep this running while you work. HEADING OS's own MCP bridge (section 5.3) exists because such tunnels drop: it hides short outages from Claude Code.

## 4. First-time setup: the CLI and the bank

### 4.1 Install the Hindsight command line

The CLI is a program called `hindsight` that talks to your server from a terminal. Install it with upstream's installer:

```bash
curl -fsSL https://hindsight.vectorize.io/get-cli | bash
```

Then point it at your server:

```bash
hindsight configure --api-url http://localhost:8888 --api-key <your-key>
```

The installer puts `hindsight` in `~/.local/bin`; set `HINDSIGHT_INSTALL_DIR` before running it to choose another folder. It warns you when that folder is not on your `PATH`.

`hindsight configure` with no options asks the questions on screen instead. The CLI reads its settings in this order, first one wins: the variables `HINDSIGHT_API_URL` and `HINDSIGHT_API_KEY`; a named profile (`--profile`, kept in `~/.hindsight/cli-profiles/<name>.toml`); the file `~/.hindsight/config`; and the default `http://localhost:8888`.

Check it:

```bash
hindsight version
hindsight health
```

### 4.2 Create the bank `main`

HEADING OS reads and writes the bank `main`. Create it once:

```bash
hindsight bank create main --name "HEADING OS"
hindsight bank list
```

## 5. Connect it to the engine

There are six parts. Part 1 switches HEADING OS's Hindsight features on. Parts 2 to 6 fill the bank and let Claude query it.

### 5.1 Store the API key where the engine looks

The engine decides that Hindsight is set up when one file exists: `~/.config/hindsight/secrets/hindsight-api-key`. Until it exists, every Hindsight hook stays silent.

1. Create the folder:

   ```bash
   mkdir -p ~/.config/hindsight/secrets
   ```

2. Create the file so only you can read it, then paste the key:

   ```bash
   (umask 077 && cat > ~/.config/hindsight/secrets/hindsight-api-key)
   ```

   Paste `<your-key>`, press Enter, then press Ctrl+D.

3. Check: `ls -l ~/.config/hindsight/secrets/hindsight-api-key` must start with `-rw-------`.

If your server runs with authentication off, the engine still needs the file at that one path to exist. Its content is then not checked by the server, so any text works, for example `none`.

### 5.2 Settings the engine reads

The defaults fit a server at `http://127.0.0.1:8888` with a bank `main`. If yours differs, set these.

**For the sync, the snapshot and `hindsight-remember.py`**, in the engine's `.env` file (`<parent>/.heading-os/.env`, never committed):

| Name | Default |
|---|---|
| `HINDSIGHT_SYNC_API_URL` | `http://127.0.0.1:8888` |
| `HINDSIGHT_SYNC_BANK` | `main` |
| `HINDSIGHT_SYNC_KEY_FILE` | `~/.config/hindsight/secrets/hindsight-api-key` |

**For the hooks that run inside Claude Code.** These read only the environment Claude Code starts with, not `.env`. So export them in your shell profile (for example `~/.bashrc`) and open a new terminal before you start `claude`:

| Name | Default | What it does |
|---|---|---|
| `HINDSIGHT_INJECT_URL` | `http://127.0.0.1:8888` | The server the per-message lookup asks. |
| `HINDSIGHT_INJECT_BANK` | `main` | The bank it asks. |
| `HINDSIGHT_INJECT_KEY_FILE` | `~/.config/hindsight/secrets/hindsight-api-key` | The key file. Its presence is what switches the hooks on. |
| `HINDSIGHT_INJECT_TIMEOUT` | `10` | Seconds to wait for Hindsight before using the snapshot. |
| `HINDSIGHT_INJECT_TOP` | `6` | How many facts at most to show Claude. |
| `HINDSIGHT_INJECT_MAX_TOKENS` | `1200` | The size limit Hindsight is asked to respect. |

### 5.3 Let Claude query the bank: the MCP server `hindsight-main`

Every Hindsight server has a built-in MCP endpoint per bank, at `http://<server>:8888/mcp/<bank>/`. HEADING OS's skills and rules call the MCP server `hindsight-main` and expect it to point at the bank `main`.

Use the engine's bridge. It is a small local program that Claude Code starts. It talks to Hindsight for Claude, and when the link drops it reconnects in the background, so Claude Code never marks the server as failed. It also keeps your key out of `~/.claude.json`.

1. Write the request header into a private file:

   ```bash
   (umask 077 && cat > ~/.config/hindsight/mcp-headers.json)
   ```

   Paste this line with your key in it, press Enter, then Ctrl+D:

   ```json
   {"Authorization": "Bearer <your-key>"}
   ```

2. Test the link. This only reads: it connects, lists the tools and closes:

   ```bash
   cd <parent>/.heading-os
   .venv/bin/python scripts/hindsight-mcp-bridge.py probe
   ```

3. Register the bridge with Claude Code. Use full paths, because Claude Code starts it from anywhere:

   ```bash
   claude mcp add --scope user --transport stdio hindsight-main -- <parent>/.heading-os/.venv/bin/python <parent>/.heading-os/scripts/hindsight-mcp-bridge.py serve
   ```

4. Restart Claude Code and check: `claude mcp get hindsight-main` says `Status: ✔ Connected`.

The bridge reads `--url` or `HINDSIGHT_MCP_BRIDGE_URL` (default `http://127.0.0.1:8888/mcp/main/`) and `--headers-file` or `HINDSIGHT_MCP_BRIDGE_HEADERS` (default `~/.config/hindsight/mcp-headers.json`). Set `HINDSIGHT_MCP_BRIDGE_DEBUG=1` to log every message.

**The direct way, without the bridge.** Upstream's form registers the endpoint over HTTP. It works, but it writes the key in plain text into `~/.claude.json`, and Claude Code gives up on the server after a few failed retries until you type `/mcp`:

```bash
claude mcp add --scope user --transport http hindsight-main http://localhost:8888/mcp/main/ --header "Authorization: Bearer <your-key>"
```

### 5.4 Keep every session: the Claude Code plugin

Hindsight's coding-agents plugin saves each Claude Code session into the bank when the session stops. It also adds knowledge pages and a few tools of its own.

1. Install it for Claude Code, pointing at your own server:

   ```bash
   npx @vectorize-io/hindsight-coding-agents install claude-code --server self-hosted --api-url http://localhost:8888
   ```

   Upstream says this adds 3 hooks to `~/.claude/settings.json`, registers an MCP server through `claude mcp add` at user scope, and installs a companion skill. The plugin's runtime lands in `~/.hindsight/coding-agents/`, and its one settings file is `~/.hindsight/coding-agent.json`.

2. Make the plugin write into the bank `main`, and only for HEADING OS. By default it gives every git repository its own bank, which HEADING OS would never read. Open `~/.hindsight/coding-agent.json`, keep the keys it already has, and set these. Use absolute paths (they start with `/`):

   ```json
   {
     "apiUrl": "http://127.0.0.1:8888",
     "apiToken": "<your-key>",
     "serverMode": "self-hosted",
     "optInOnly": true,
     "optInPaths": ["<parent>/.heading-os", "<parent>/.heading-os-data"],
     "mapPathToBank": {
       "<parent>/.heading-os": "main",
       "<parent>/.heading-os-data": "main"
     },
     "manageBankConfig": false,
     "gitIngest": "none"
   }
   ```

   What each key does, from the plugin's README:

   - `apiUrl`: where your server is. `apiToken`: the key it sends.
   - `optInOnly` with `optInPaths`: the plugin works only in these folders and stays inert everywhere else, with no bank created.
   - `mapPathToBank`: sessions in these folders go to the bank `main`. Yards are git worktrees of the engine, and the plugin gives a worktree the same bank as its main checkout, so yards land in `main` too.
   - `manageBankConfig: false`: the plugin does not change the settings of your bank.
   - `gitIngest: "none"`: the plugin does not copy git history into the bank. The engine's sync already sends your files.

3. Restart Claude Code.

Each session is then stored when it stops, as a document whose id starts with `conversation:`.

### 5.5 Fill the bank with your files: the sync

`scripts/hindsight-sync.py` sends the text of your data repository into the bank. Each file becomes one document whose id is its path, so sending it again replaces it. A file over 200,000 bytes is sent in parts. A file that left the repository has its document removed on a later full run.

The sync remembers what it sent in a ledger file, `ledger.jsonl`, one JSON line per file sent or removed. The ledger is what lets the next run send only what changed. The sync prints its ledger lines, and you append them to the ledger yourself, exactly as the timer does.

Run these in the engine folder:

```bash
cd <parent>/.heading-os
```

1. See what would be sent and what is skipped. Nothing is sent:

   ```bash
   .venv/bin/python scripts/hindsight-sync.py plan --wave all
   ```

2. Find the ledger's folder and make sure it exists:

   ```bash
   LEDGER_DIR="$(.venv/bin/python scripts/data-path.py path runtime:hindsight-sync/state)"
   mkdir -p "$LEDGER_DIR"
   ```

3. A dry run. It sends, deletes and writes nothing:

   ```bash
   .venv/bin/python scripts/hindsight-sync.py push --wave all --limit 50 --dry-run
   ```

4. Send the first 50 files, and record them:

   ```bash
   .venv/bin/python scripts/hindsight-sync.py push --wave all --limit 50 >> "$LEDGER_DIR/ledger.jsonl"
   ```

5. Compare the ledger with the server:

   ```bash
   .venv/bin/python scripts/hindsight-sync.py status
   ```

Repeat step 4 until everything is sent. Each document takes the server some time to read with the AI model, so send in portions. `--max-pending 80` makes a run send nothing while the server still has 80 jobs waiting. `--limit 0` means no limit.

Why `--wave all` and not `--wave core`: the sync can send a list of important folders first (the `core` wave), but only when you have written that list into a file `wave-core.txt` beside the ledger. Without that file every file belongs to `rest`, and `--wave core` sends nothing.

**Run it every 15 minutes (Linux and WSL2).** In the engine's main folder, not in a yard (it refuses there):

```bash
bash scripts/install-hindsight-sync-timer.sh
```

It installs systemd user timers: the sync at minutes 07, 22, 37 and 52 of every hour, with `--limit 200 --max-pending 80`, appending to the ledger; and a daily check at 06:40 (`capture-check`) that every interactive session of the last 25 hours reached the bank. Change the two numbers with `HINDSIGHT_SYNC_LIMIT=... HINDSIGHT_SYNC_MAX_PENDING=...` in front of the command. Remove it with `bash scripts/install-hindsight-sync-timer.sh --uninstall`. On WSL2, systemd must be switched on (see [INSTALL.md](INSTALL.md)).

### 5.6 Keep a daily snapshot (recommended)

Some things exist only in Hindsight: the plugin's session documents, memories saved without a file, and what Hindsight builds by itself (observations, mental models, directives, knowledge pages). The snapshot copies them to disk, so you have them even when the server is down or lost. It does not copy the documents of your files, because the files are the copy.

```bash
cd <parent>/.heading-os
.venv/bin/python scripts/hindsight-snapshot.py take
.venv/bin/python scripts/hindsight-snapshot.py status
```

`take` only reads from the server (every request is a GET) and replaces the previous snapshot whole; a failed run keeps the previous one. `status` prints the snapshot's date and counts. It is written into your data repository; this prints where:

```bash
.venv/bin/python scripts/data-path.py path runtime:hindsight-snapshot/cache
```

**Run it daily (Linux and WSL2)**, in the engine's main folder: `bash scripts/install-hindsight-snapshot-timer.sh`. It runs at 04:10 in your configured time zone. Remove it with `--uninstall`.

## 6. What changes in your sessions

Once the key file from 5.1 exists, three things happen in every Claude Code session in HEADING OS. `bash scripts/setup-platform.sh`, which INSTALL.md already ran, registered the hooks; nothing else is needed.

**1. Every message is looked up.** When you send a message, a hook (`hindsight-recall-inject.py`) asks the bank about it, waits up to 10 seconds, and gives Claude up to 6 facts, each with the document it came from. At the same moment it searches the snapshot; if Hindsight does not answer in time, the snapshot's answer is used and marked "from snapshot of <date>".

**2. The first look into your records goes to Hindsight first.** In each session, the first attempt to search or read your memory records directly is refused until Claude has asked Hindsight once. Any query through an MCP server whose name contains `hindsight`, or the CLI command `hindsight memory recall`, counts. An attempt that fails also counts, so a server that is down never locks a session.

**3. Answers show their memory check.** When the lookup found facts, the answer must show what Hindsight said: a block that starts with `From Hindsight memory`, or a line that starts with `Hindsight:`, or the words `from snapshot of`. If the answer leaves this out, a hook (`hindsight-shown.py`) sends Claude back once to add it.

A recalled fact is a pointer, not proof. Before Claude acts on anything important, it opens the file the fact names.

## 7. Daily use, with worked examples

### 7.1 Ask about the past

You type in a session:

> What did we decide about the pricing for the pilot?

You see a block that starts "From Hindsight memory", with the facts Hindsight found and the files they came from, and then Claude's answer, checked against those files.

### 7.2 Recall from a terminal

```bash
hindsight memory recall main "pricing for the pilot"
```

Useful options: `--budget low|mid|high` (how hard it searches; default `mid`), `--max-tokens 2000` (how much it returns; default `4096`), `-o json` for machine-readable output.

### 7.3 Remember something

When an answer settles something a later session will need, Claude offers, at the end of the answer: "Remember: <one sentence>?" Say yes. Claude writes the memory as a small file in the `auto-memory` folder of your data repository, with a line in its header saying who wrote it (`written_by: operator` when you dictated it, `helm` when Claude did). The next sync run sends it into Hindsight. The file is the record; Hindsight holds a searchable copy.

For a memory that should have no file, the engine has a direct command:

```bash
cd <parent>/.heading-os
.venv/bin/python scripts/hindsight-remember.py --id renewal-month "The domain renews every March. Renew it in February."
```

It stores the document `memory:renewal-month`, waits for the server, reads it back, and reports how many facts Hindsight extracted. It exits 2 when Hindsight extracted none, because a memory with no fact can never be found. It refuses an id that already exists unless you add `--replace`. `--tag <tag>` adds a tag; `--file <note.md>` reads the memory from a file.

### 7.4 When the server is down

```bash
cd <parent>/.heading-os
.venv/bin/python scripts/hindsight-snapshot.py lookup "pricing for the pilot"
.venv/bin/python scripts/hindsight-snapshot.py query "pricing for the pilot"
```

`lookup` asks Hindsight first and the snapshot second. `query` reads only the snapshot. Anything newer than the snapshot's date is not in it.

### 7.5 Look around in the web page

Open `http://localhost:9999`. You can browse banks, documents, memories and operations.

## 8. Check that it works

Run these in order. Each one checks a different link.

1. The server answers: `hindsight health` and `hindsight version`.
2. The bank exists: `hindsight bank list` shows `main`.
3. The key is in place: `ls -l ~/.config/hindsight/secrets/hindsight-api-key` starts with `-rw-------`.
4. The bridge reaches the server: `.venv/bin/python scripts/hindsight-mcp-bridge.py probe` (in `<parent>/.heading-os`).
5. Claude Code sees both MCP servers: `claude mcp list` shows `hindsight-main` and the plugin's server as connected. In a session, `/mcp` lists their tools.
6. The sync knows its state: `.venv/bin/python scripts/hindsight-sync.py status`.
7. Sessions arrive: after a few sessions, `.venv/bin/python scripts/hindsight-sync.py capture-check` exits 0. It exits 1 and names the session when one is missing.
8. The snapshot exists: `.venv/bin/python scripts/hindsight-snapshot.py status`.
9. The whole loop: in a new session, ask about something you know is in your records. The answer shows a "From Hindsight memory" block or a "Hindsight:" line.

Each engine script also has a `self-test` that runs against an invented bank on your own computer and touches no real server: `hindsight-sync.py self-test`, `hindsight-remember.py self-test`, `hindsight-snapshot.py self-test` and `hindsight-mcp-bridge.py self-test`.

## 9. Common problems and fixes

| What you see | What it means | What to do |
|---|---|---|
| No "From Hindsight memory" block ever appears | The key file is missing, so the hooks are off. | Do section 5.1. |
| Every answer says the snapshot answered | The hook cannot reach the server in 10 seconds. | Check `hindsight health`. If your server is not at `127.0.0.1:8888`, export `HINDSIGHT_INJECT_URL` before starting `claude`. If it is slow, raise `HINDSIGHT_INJECT_TIMEOUT`. |
| `401` or `Unauthorized` | The key sent does not match the server's `HINDSIGHT_API_TENANT_API_KEY`. | Compare the key file, `mcp-headers.json`, the CLI's configuration and the plugin's `apiToken` with the server's key. |
| `claude mcp get hindsight-main` says failed | The bridge cannot start or cannot reach the server. | Run `scripts/hindsight-mcp-bridge.py probe` and read its message. Check the paths in the `claude mcp add` line are absolute. |
| A session does not appear in the bank | The plugin is not set up for that folder, or it writes to another bank. | Check `optInPaths` and `mapPathToBank` in `~/.hindsight/coding-agent.json` (section 5.4). `capture-check` names the missing sessions. |
| The plugin made banks like `coding-agent::...` | `mapPathToBank` is missing. | Add it (section 5.4). Those extra banks are not used by HEADING OS. |
| The first message of each session waits a long time | The plugin runs a reflect on the first message and Claude waits for it. | Run the plugin's prompt hook in the background through the engine's wrapper: in `~/.claude/settings.json`, in the `UserPromptSubmit` entry whose command ends in `claude-hook.js`, put `python3 <parent>/.heading-os/.claude/hooks/hindsight-hint-async.py -- ` in front of the command and add `"async": true` to that entry. Or set `"autoInject": "none"` in `~/.hindsight/coding-agent.json`. |
| `push --wave core` sends nothing | There is no `wave-core.txt`, so every file is in `rest`. | Use `--wave all`. |
| The sync sends the same files on every run | The ledger is not being written. | Append the push output to the ledger (section 5.5, step 4). |
| The server's queue grows and grows | You sent more than it can read with the AI model. | Wait, and send with `--max-pending 80`. Watch the queue with `hindsight operation list main`. |
| `hindsight-remember.py` exits 2 | Hindsight extracted no fact from your text. | Write the memory as plain, full factual sentences and send it again with `--replace`. |
| A timer installer says `systemctl not found` | systemd is off in WSL2, or you are on macOS. | On WSL2, switch systemd on (see INSTALL.md). On macOS, run the commands by hand (section 11). |
| The Docker container stops with `Permission denied` | You used a host folder instead of the named volume. | Use the named volume `hindsight-data` exactly as in 3.2. |

## 10. Update and remove

### Update

- **The server (Docker).** The command in 3.2 has `--pull always`, so removing the container and running the same command again fetches the newest image. Your data is in the `hindsight-data` volume and stays. `docker stop hindsight && docker rm hindsight`, then the `docker run` line again.
- **The server (Python).** `pip install -U hindsight-api`, then start `hindsight-api` again.
- **The CLI.** Run the installer line from 4.1 again; it replaces the program in place. Then `hindsight --version`.
- **The plugin.** It updates its own runtime once a day by default (`autoUpdate`). `npx @vectorize-io/hindsight-coding-agents update` refreshes it now. Running `install claude-code` again re-wires the hooks after a release that adds one; it does not ask for the server again.
- **The engine's parts** update with HEADING OS itself.

Read upstream's release notes before a big jump in versions.

### Remove

1. Take a last snapshot if you may want it: `.venv/bin/python scripts/hindsight-snapshot.py take`.
2. Remove the timers, in the engine's main folder: `bash scripts/install-hindsight-sync-timer.sh --uninstall` and `bash scripts/install-hindsight-snapshot-timer.sh --uninstall`.
3. Remove the plugin: `npx @vectorize-io/hindsight-coding-agents uninstall claude-code`. Upstream says it removes exactly what install added.
4. Remove the MCP server: `claude mcp remove hindsight-main -s user`.
5. Switch the hooks off: delete `~/.config/hindsight/secrets/hindsight-api-key`. The per-message lookup, the first-lookup rule and the answer check then stay silent.
6. Delete `~/.config/hindsight/mcp-headers.json`.
7. Only if you are sure: stop the server and delete its data. With Docker: `docker stop hindsight && docker rm hindsight && docker volume rm hindsight-data`. This deletes every memory that is not in your files or your snapshot.

HEADING OS keeps working without Hindsight. Your files are untouched.

## 11. What this guide does not cover yet

- **Scheduling on macOS.** The engine's timer installers use systemd, which macOS does not have. On a Mac, run the sync (5.5) and the snapshot (5.6) by hand. The engine has no macOS scheduler for them.
- **The Docker and Python routes were not run for this guide.** Their commands are upstream's own, from its 1.1 installation page; HEADING OS itself runs a server on a separate machine.
- **The plugin's settings** are taken from its README (version 0.8.2) and from a working HEADING OS install, not from every combination.
- **Engine tools for a remote server's health and backups** (`scripts/hindsight-health.py`, `scripts/backup-pull.py`, `scripts/hindsight-snapshot-offsite.py`) exist, but each is tied to one way of hosting the server and is not described here. Their settings are listed, empty, in `.env.example`.
