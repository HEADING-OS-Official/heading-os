# Lific: the issue tracker

This guide takes you from nothing to daily use of Lific with HEADING OS. It assumes you have never seen Lific before. Lific is optional. HEADING OS runs without it, and every part that uses it stays silent when it is not installed.

Every command, flag, file name and default in this guide was checked against Lific 2.11.1 (its own `--help`), its upstream README, or the HEADING OS engine source. Where something could not be checked, the guide says so.

## Contents

1. [What Lific is, and why HEADING OS uses it](#1-what-lific-is-and-why-heading-os-uses-it)
2. [What you need first](#2-what-you-need-first)
3. [Install Lific](#3-install-lific)
4. [First-time setup](#4-first-time-setup)
5. [Connect it to the engine](#5-connect-it-to-the-engine)
6. [How HEADING OS uses Lific](#6-how-heading-os-uses-lific)
7. [Daily use, with worked examples](#7-daily-use-with-worked-examples)
8. [Back up and restore](#8-back-up-and-restore)
9. [Check that it works](#9-check-that-it-works)
10. [Common problems and fixes](#10-common-problems-and-fixes)
11. [Update and remove](#11-update-and-remove)
12. [What this guide does not cover yet](#12-what-this-guide-does-not-cover-yet)

## 1. What Lific is, and why HEADING OS uses it

Lific is a small issue tracker. An "issue" is one piece of work with a title, a status and a history of comments. Lific keeps everything in one SQLite file on your own computer. SQLite is a database that is just a file: there is no database server to run.

Lific comes as one program called `lific`. That one program gives you three ways in:

- **A command line (CLI).** You type `lific issue list ...` in a terminal.
- **An MCP server.** MCP (Model Context Protocol) is how Claude Code talks to outside tools. Through it, Claude can read and write issues for you.
- **A web page**, served at `http://localhost:3456` when the background service runs.

Its source is `https://github.com/VoidNullable/lific`, under the Apache-2.0 licence.

**Why HEADING OS uses it.** Claude forgets everything when a session ends. Lific is where work that must outlive a session is written down: "do this after that", "don't forget this", "a yard is working on this now". The next session, or a different session, can read it back.

HEADING OS uses a small part of Lific on purpose: **projects, issues, statuses, comments and `blocks` links.** It does not use Lific's plans, pages, folders, labels or attachments. Plans and design notes stay as files in your data repository.

A simple test decides what goes into Lific. Ask: would finishing this work mean rewriting a text, or only moving a status? If you would rewrite a text, it is a document, a plan or a thread. If you would only move a status, it is a Lific issue.

## 2. What you need first

- **HEADING OS installed**, following [INSTALL.md](INSTALL.md). This guide calls the parent folder `<parent>`. The engine is `<parent>/.heading-os` and your data repository is `<parent>/.heading-os-data`.
- **Claude Code** working in a terminal (`claude --version` prints a version).
- **A terminal on Linux, WSL2 or macOS.** On Windows, everything in this guide runs inside WSL2.
- **`curl`** to download the program (Linux and WSL2), or **Homebrew** on macOS (`brew --version` prints a version).
- **For the background service on WSL2: systemd switched on.** If `systemctl --user status` says `System has not been booted with systemd`, follow the `systemctl not found` line in the troubleshooting table of [INSTALL.md](INSTALL.md). You can also skip the service; section 4 shows how.

You do not need Rust, Docker or any database server.

## 3. Install Lific

Pick one of the three ways. The release binary is the fastest on Linux and WSL2. Homebrew is the simplest on macOS.

### 3a. Linux or WSL2: the release binary (recommended)

1. Find your processor type:

   ```bash
   uname -m
   ```

   `x86_64` means you want `lific-linux-x86_64`. `aarch64` means you want `lific-linux-aarch64`.

2. Make a fresh, empty folder for the download and go into it:

   ```bash
   mkdir -p ~/Downloads/lific-download && cd ~/Downloads/lific-download
   ```

3. Download the program and the checksum list from the latest release. Replace `lific-linux-x86_64` if `uname -m` said `aarch64`:

   ```bash
   curl -sSLO https://github.com/VoidNullable/lific/releases/latest/download/lific-linux-x86_64
   curl -sSLO https://github.com/VoidNullable/lific/releases/latest/download/sha256sums.txt
   ```

4. Check that the download is not damaged or tampered with. This line must print `lific-linux-x86_64: OK`:

   ```bash
   grep ' lific-linux-x86_64$' sha256sums.txt | sha256sum -c -
   ```

   If it prints `FAILED`, delete both files and download again. Never skip this step.

5. Put the program in your personal programs folder under the name `lific`, and make it runnable:

   ```bash
   mkdir -p ~/.local/bin
   mv lific-linux-x86_64 ~/.local/bin/lific
   chmod +x ~/.local/bin/lific
   ```

6. Confirm the shell finds it:

   ```bash
   lific --version
   ```

   It prints a line like `lific 2.11.1`. If it says `command not found`, `~/.local/bin` is not on your `PATH`. Open a new terminal, or add `export PATH="$HOME/.local/bin:$PATH"` to the end of `~/.bashrc` and open a new terminal.

The release files are `lific-linux-x86_64`, `lific-linux-aarch64`, `lific-macos-aarch64`, `lific-macos-x86_64`, `lific-windows-x86_64.exe` and `sha256sums.txt` (checked on the v2.11.1 release page).

### 3b. macOS: Homebrew (recommended)

```bash
brew install voidnullable/tap/lific
lific --version
```

Upstream says this works on macOS and Linux and needs no Rust toolchain.

If you prefer the release binary on a Mac, follow 3a with `lific-macos-aarch64` (Apple Silicon, M1 and later) or `lific-macos-x86_64` (Intel). macOS has no `sha256sum` by default; use `shasum -a 256 -c -` in step 4 instead.

### 3c. Any system: build from source with Rust

If you already have Rust installed:

```bash
cargo install lific --locked
```

This compiles Lific, which takes several minutes.

## 4. First-time setup

### 4.1 Create your Lific instance

An "instance" is your own copy of Lific: a settings file, a database and your user account. Create it with one command:

```bash
lific init
```

What it does, in Lific's own words: it "writes lific.toml (kept if already present), creates the database and your administrator account, installs a background service (systemd user unit on Linux, LaunchAgent on macOS) and starts it, then waits until the server answers." Running it again is safe and repairs whatever is missing.

It asks you two things:

- **Your name**, for your administrator account.
- **An auth mode.** `login-free` means the web page needs no password. `passwords` means it asks for one. The engine works the same with either.

Answer on screen. Do not pass `--password` on the command line: that would leave the password in your shell history.

**Where the files go on Linux and WSL2:** the settings in `~/.config/lific/lific.toml`, and the data in `~/.local/share/lific/`. Lific's help says macOS uses the macOS equivalents of these folders; it does not name them, and this guide has not checked them on a Mac.

**If you do not want a background service,** run `lific init --no-service` instead. Everything HEADING OS does still works: the engine and Claude both open the database file directly. Only the web page needs the service. Run `lific start` in a terminal when you want the web page, and stop it with Ctrl+C.

**After setup, check two things:**

1. Lific is healthy:

   ```bash
   lific doctor
   ```

   It prints a pass, warn or fail line per check, and exits 0 when nothing failed. Checks that need the server are skipped, not failed, when no server runs.

2. The server listens only on your own computer, not on your network. Open `~/.config/lific/lific.toml` and look at the `[server]` section. The line `host = "127.0.0.1"` means only this computer can reach it. If it says `host = "0.0.0.0"`, every machine on your network can reach the web page; change it to `"127.0.0.1"`, then run `lific service restart`.

### 4.2 Create your first project

Every issue lives in a project. A project has a name and a short key of at most 5 capital letters, such as `WORK`. The key becomes the start of every issue number in it, so issues in a project with key `WORK` are called `WORK-1`, `WORK-2`, and so on.

```bash
lific project create --name "My work" --identifier WORK --description "Everything I track with Claude"
lific project list
```

Use any name and key you like. This guide uses `<KEY>` from now on for your project key.

### 4.3 Give Claude its own account (a "bot")

When Claude writes in Lific, the history should say "the bot did this", not "you did this". Lific records who made every change. Without a separate account, every change Claude makes would carry your name.

So you create a bot user, give it an API key, and let it into your project.

1. Create the bot. Pick any username; this guide uses `<bot>`. Lific prompts for a password when you leave `--password` out:

   ```bash
   lific user create --username <bot> --email <bot>@example.invalid --bot
   ```

2. Give the bot a role in your project. The roles are `viewer`, `maintainer` and `lead`; the default is `viewer`, which can only read. HEADING OS runs its own bot as `maintainer`, and in Lific 2.11.1 that role creates issues, writes comments and changes statuses:

   ```bash
   lific member add --project <KEY> --user <bot> --role maintainer
   ```

   On a new install Lific checks membership on every call, so a user who is not a member of a project cannot even see it. Use `--all` instead of `--project <KEY>` to add the bot to every project you have.

3. Create an API key for the bot. An API key is a long password for programs:

   ```bash
   lific key create --name claude-code --user <bot>
   ```

   Copy the key it shows and keep it for section 5.2. `lific key list` never shows a key again, only its name. Add `--expires 2027-12-31` if you want the key to stop working on a date; without it, the key never expires.

## 5. Connect it to the engine

There are two connections. Claude Code needs the MCP server, so Claude can read and write issues in a session. The engine's scripts need nothing extra: they call the `lific` program directly.

### 5.1 Do not use `lific connect`

> **Warning:** do not run `lific connect`, in any form, including `--dry-run`. HEADING OS tested it: it rewrites the whole of `~/.claude.json`, the file that holds every project's Claude Code settings on your computer, and by default it registers a connection over HTTP with the API key written in plain text in that file.

Connect by hand instead. It takes four steps.

### 5.2 Store the bot's key in a private file

1. Make sure the folder exists:

   ```bash
   mkdir -p ~/.config/lific
   ```

2. Create the file so only you can read it, then paste the key:

   ```bash
   (umask 077 && cat > ~/.config/lific/agent-token)
   ```

   Paste the key, press Enter, then press Ctrl+D.

3. Confirm the permissions:

   ```bash
   ls -l ~/.config/lific/agent-token
   ```

   The line must start with `-rw-------`. If not, run `chmod 600 ~/.config/lific/agent-token`.

### 5.3 Write a small launcher

A launcher is a tiny script that Claude Code starts. It reads the key from the private file, hands it to Lific in the `LIFIC_TOKEN` variable, and starts Lific's MCP server. This keeps the key out of `~/.claude.json`.

It also refuses to start when the key file is missing. Without the key, Lific would run Claude as you, with only a warning on stderr.

Create `~/.local/bin/lific-mcp-agent` with this content:

```sh
#!/bin/sh
# Start Lific's MCP server as the bot, never as you.
set -eu

TOKEN_FILE="${LIFIC_TOKEN_FILE:-$HOME/.config/lific/agent-token}"

if [ ! -r "$TOKEN_FILE" ]; then
    echo "lific-mcp-agent: no readable token at $TOKEN_FILE; refusing to start as you." >&2
    exit 1
fi

LIFIC_TOKEN="$(cat "$TOKEN_FILE")"
export LIFIC_TOKEN

exec lific mcp "$@"
```

Make it runnable:

```bash
chmod +x ~/.local/bin/lific-mcp-agent
```

`lific mcp` finds `lific.toml` in its standard place by itself, the same way the engine's scripts do. If you created your instance somewhere else (with `--here` or `--config`), change the last line to `exec lific mcp --config /full/path/to/lific.toml "$@"`.

### 5.4 Register it with Claude Code

```bash
claude mcp add --scope user --transport stdio lific -- ~/.local/bin/lific-mcp-agent
```

`--scope user` makes the server available in every folder, so it also works in yards (the separate work folders described in [YARDS.md](YARDS.md)). `stdio` means Claude Code starts the launcher itself and talks to it directly; no network and no running Lific server are needed.

Restart Claude Code, then check:

```bash
claude mcp get lific
```

It must say `Status: ✔ Connected`.

### 5.5 Settings the engine reads

The local setup above needs none of these. Put them in the engine's `.env` file (`<parent>/.heading-os/.env`) only when you need them. `.env` is never committed.

| Name | What it does | When you need it |
|---|---|---|
| `LIFIC_BOT_USER` | The bot username the `[dossier]` journal posts as (section 6.4). | Set it to `<bot>` if you want the journal. Unset means no journal record is ever sent. |
| `HEADING_OS_LIFIC_DB` | Points every engine call to `lific` at another database file. | Testing on a copy of your database. |
| `HEADING_OS_LIFIC_CONFIG` | The `lific.toml` the backup script reads. Default `~/.config/lific/lific.toml`. | Your settings file is somewhere else. |
| `LIFIC_URL` | The address of a Lific server reached over HTTP. | Only for a Lific on another machine. |
| `LIFIC_API_KEY` | An API key for that server. | Same. |
| `LIFIC_TOKEN` | The bot's key for HTTP or MCP. | Not needed with the launcher above, which sets it. |

## 6. How HEADING OS uses Lific

### 6.1 The engine parts that talk to Lific

| Part | What it does |
|---|---|
| `scripts/lific-board.py` | Reads the board for `/prime`: how many issues are `backlog`, `todo` and `active` per project, and which issues ask you to close them by hand. It only reads. |
| `scripts/dossier-journal.py` | Posts short `[dossier]` comments when a prompt is saved, a handoff is saved, a brief is sent, or a task closes (section 6.4). |
| `scripts/lific-backup.py` | Writes one `lific dump` archive into your data repository, so your backup carries the tracker (section 8). |
| `lific git-hook` | Lific's own command. It closes the issues a merge commit names with `closes <KEY>-<number>` (section 7.6). |
| `/dossier <KEY>-<number>` | A skill that gathers everything about one issue: prompts, handoffs, briefs, journal records, comments and commits. Read-only. |

When `lific` is not installed, `lific-board.py` and `lific-backup.py` print nothing useful and exit 0, and `/prime` shows no Lific panel.

### 6.2 What the five statuses mean here

Lific's own documentation describes `todo` and `active` with the same words. HEADING OS gives each status one meaning:

| Status | Meaning in HEADING OS | Who moves it there |
|---|---|---|
| `backlog` | Written down, not yet looked at by you. New issues start here. | Claude, when it creates the issue |
| `todo` | You approved it. Ready to hand to a yard. | You |
| `active` | Someone holds it: a yard, or your main session. | The main session, when it sends the work to a yard |
| `done` | Merged into `main`, or closed by hand when there was nothing to merge. | The merge, or you |
| `cancelled` | Dropped. | You |

Claude never creates an issue at `todo`. Deciding that work is real is your decision, and `backlog` is your inbox.

### 6.3 The rules Claude follows

These live in the engine, in `.claude/rules/lific.md`, and load in every session. The ones you will notice:

- **"Don't forget this" becomes an issue.** When you say "don't forget to do this", "we will do this after that", or anything of that shape, Claude creates an issue. It searches first for one that already says the same thing, puts it in the right project, and leaves it at `backlog`.
- **"After that task" becomes a `blocks` link.** Lific then knows the second issue cannot start until the first is `done`. Note: an issue whose blocker is `cancelled` is still blocked. Close a dead blocker as `done`, or remove the link.
- **A yard only adds.** A yard may add comments and create new issues. Changing a status, linking, bulk edits and deleting belong to your main session.
- **A yard writes two comments on every issue it takes.** One when it starts (its name and its branch, because Lific has no "who holds this" field that HEADING OS uses). One when it finishes: what was done, what was not done, and a closing line (section 7.6).
- **Comments go through MCP, never the CLI.** `lific comment add` without `--user` signs the comment as the first administrator, which is you. MCP signs it as the bot.
- **No secrets in Lific, ever.** A deleted issue keeps its full text for the number of days in the `trash_days` setting of `lific.toml` (30 in the install HEADING OS was measured on), and the audit history keeps its rows with nothing that removes them. An issue cannot be unwritten.

### 6.4 The `[dossier]` journal

The journal is a short trail of comments, added in HEADING OS in October 2026, on each issue a session works on. It answers "what happened to this task, and where are the files?" without opening every file.

A record is one comment, posted as the bot. It has a head line, up to 3 summary lines of at most 160 characters each, and the path of a file in your data repository, never the full text:

```text
[dossier] 2026-01-01 09:00 UTC · handoff · manual · session 0000aaaa
Next action: run the example step.
Awaiting approval: nothing.
Already done (do not repeat): the example draft.
→ <path of the handoff, relative to the data repository>
```

Four events write a record: a prompt saved, a handoff saved, a brief sent, a task closed.

How it is sent: every record first goes into an outbox file in your data repository, one JSON line per record. A separate step, `dossier-journal.py flush`, sends what waits there. `/prime` runs that step, so most people never touch it.

To turn the journal on:

1. Set the bot username in the engine's `.env`:

   ```bash
   LIFIC_BOT_USER=<bot>
   ```

2. Count what waits in the outbox:

   ```bash
   cd <parent>/.heading-os
   .venv/bin/python scripts/dossier-journal.py count
   ```

3. Send it by hand if you want to see it work:

   ```bash
   .venv/bin/python scripts/dossier-journal.py flush
   ```

   It prints a JSON summary and exits 1 when records are left over.

Records that Lific refuses are moved to a `rejected` file beside the outbox, never deleted, and `/prime` names them.

## 7. Daily use, with worked examples

Most of the time you do not type Lific commands at all. You talk to Claude, and `/prime` shows you the board. The commands below are for when you want to look or act yourself. Every one works from any folder.

### 7.1 Ask Claude to remember something

You type in a session:

> Don't forget: we need to renew the domain before the end of the month.

Claude searches Lific for a similar issue, creates one in the right project at `backlog`, and tells you its number.

### 7.2 See what is open

```bash
lific issue list --project <KEY>
lific issue list --project <KEY> --status todo
lific issue list --project <KEY> --workable
```

`--workable` shows only open issues with no unfinished blocker. The default is 50 results; `--limit 200` shows more.

Or, in a session, run `/prime` and read the Lific panel. It shows the counts per status and the issues waiting for you to close them.

### 7.3 Approve an issue

You approve an issue by moving it from `backlog` to `todo`:

```bash
lific issue update <KEY>-12 --status todo
```

Or tell Claude in your main session: "Approve <KEY>-12."

### 7.4 Create an issue yourself

```bash
lific issue create --project <KEY> --title "Renew the domain" --priority high --description "Before the end of the month."
```

The status starts at `backlog`; the priorities are `urgent`, `high`, `medium`, `low` and `none` (the default).

### 7.5 Read one issue and its comments

```bash
lific issue get <KEY>-12
lific comment list <KEY>-12
```

`comment list` shows the newest first, 50 at a time; add `--order asc` for oldest first, and `--offset 50` for the next page.

For the whole story of one issue across files, Lific and git, type `/dossier <KEY>-12` in a session.

### 7.6 Close an issue

**Work that ends in a merge.** The main session's merge commit carries one line per finished issue:

```text
closes <KEY>-12
```

`fixes` and `resolves` work too, in any capitals. A bare mention such as "see <KEY>-12" closes nothing. After the merge, the main session feeds that one commit message to Lific:

```bash
git log -1 --format=%B <merge-commit> | lific git-hook
```

Add `--dry-run` to see what would close without writing anything.

Do not use `lific git-hook --range ...`. The hook remembers nothing, so a range re-reads older `closes` lines and closes again any issue you reopened by hand.

**Work with nothing to merge** (a decision, research, a document). No commit will close it, so `git-hook` never sees it. The yard instead starts its last comment with a fixed sentence, alone on the first line. `/prime` lists every issue whose comment starts that way, and you close it:

```bash
lific issue update <KEY>-12 --status done
```

The exact sentence is held in one place in the engine. Print it with:

```bash
grep -n '^DONE_REQUEST_MARKER' <parent>/.heading-os/scripts/lific-board.py
```

### 7.7 Search

```bash
lific search "domain" --project <KEY>
```

It searches issues, pages and comments, and matches words rather than an exact phrase.

### 7.8 The web page

When the background service runs, open `http://localhost:3456` in a browser. It shows the same data as the CLI, from the same SQLite file. A bot account does not appear in the web page's user list; `lific user list` in the terminal shows it.

## 8. Back up and restore

Lific writes its own backup archive every hour and keeps 24 (the `[backup]` section of `lific.toml`). Those stay on your computer.

To make your HEADING OS backup carry the tracker, run this in the engine's main folder (not in a yard; it refuses there):

```bash
cd <parent>/.heading-os
.venv/bin/python scripts/lific-backup.py
```

It writes one archive, `lific-backup.tar.gz`, into your data repository, replacing the previous one, and prints where. Your next backup of the data repository takes it along. It exits 0 when the dump was written (or when Lific is not installed), 1 when the dump failed (the old one is kept), and 2 when it refused to run.

Options: `--out <path>` writes somewhere else, `--config <path>` names another `lific.toml`, `--quiet` prints one line.

**A one-off archive by hand:**

```bash
lific dump --out ~/lific-backups/
```

This is safe while Lific runs. The archive holds the database, every attachment and a `manifest.json`, and only you can read it.

**Restore.** Stop the server first; restoring under a running server corrupts it:

```bash
lific service stop
lific restore <archive>.tar.gz --force
lific service restart
```

`--force` is needed when a database already exists; the current one is moved aside to `lific.db.pre-restore-<timestamp>`, not deleted.

## 9. Check that it works

Run these in order. Each one tells you something different.

1. The program runs: `lific --version`.
2. The instance is healthy: `lific doctor` exits 0.
3. The database answers: `lific project list` shows your project.
4. The bot is set up: `lific user list` shows `<bot>` marked as a bot, and `lific member list --project <KEY>` shows it as `maintainer`.
5. Claude Code sees the server: `claude mcp get lific` says `Connected`.
6. Claude acts as the bot. In a session, ask: "Add a comment 'test' to <KEY>-1 in Lific." Then run `lific comment list <KEY>-1`. The new comment's author must be `<bot>`, not you.
7. The engine reads the board:

   ```bash
   cd <parent>/.heading-os
   .venv/bin/python scripts/lific-board.py
   ```

   Exit 0 means it read cleanly. Exit 2 means `lific` is installed and a command failed; the message says which.

8. Optional, the journal: `.venv/bin/python scripts/dossier-journal.py count` prints a number.

## 10. Common problems and fixes

| What you see | What it means | What to do |
|---|---|---|
| `lific: command not found` | `~/.local/bin` is not on your `PATH`. | Open a new terminal, or add `export PATH="$HOME/.local/bin:$PATH"` to `~/.bashrc`. |
| The checksum line prints `FAILED` | The download is broken or not the file the release published. | Delete both files, download again, check again. Do not run a binary that fails. |
| `lific init` fails on WSL2 while installing the service | systemd is off in WSL2. | Switch systemd on (see [INSTALL.md](INSTALL.md)), or run `lific init --no-service`. |
| A first `lific init` run from a script fails with a confusing error | Lific needs a terminal to ask for the auth mode. | Run it in a normal terminal, or pass `--auth-mode login-free` or `--auth-mode passwords`, and `--name "<your name>"`. |
| `claude mcp get lific` says failed, and the launcher prints `no readable token` | The key file is missing or not readable. | Redo section 5.2. |
| Comments made by Claude show your name | Claude runs Lific without the bot's key. | Check that `claude mcp get lific` shows the launcher as its command, not `lific` itself. |
| Claude says it cannot see the project | The bot is not a member of it. | `lific member add --project <KEY> --user <bot> --role maintainer`. |
| Claude says a parameter is unknown | Since 2.10, every Lific MCP tool refuses parameters it does not know. | Restart the Claude Code session so it reloads the tool list. |
| A blocked issue never becomes workable | Its blocker is `cancelled`, and a cancelled blocker still blocks. | Close the blocker as `done`, or unlink the two issues from your main session. |
| An issue you reopened closes itself again | Someone ran `lific git-hook --range ...`. | Feed only the one merge commit's message (section 7.6). |
| `lific-backup.py` says `refused` | It ran in a yard or without a data repository. | Run it in `<parent>/.heading-os`. |
| `lific-backup.py` says the config is not at a path | Your `lific.toml` is somewhere else. | Set `HEADING_OS_LIFIC_CONFIG` in `.env`, or pass `--config`. |
| `/prime` shows journal records stuck or refused | Lific refused them, usually because the bot is missing from a project or `LIFIC_BOT_USER` is unset. | Fix the membership or the variable; the next `/prime` sends them. |
| A session started before an upgrade misbehaves | Each session runs its own `lific mcp`, and an old one keeps the old program. | Restart that session. |

## 11. Update and remove

### Update

1. Read the release notes on `https://github.com/VoidNullable/lific/releases` first. A new version may change the database, and an older Lific then refuses it.
2. Back up: `lific dump --out ~/lific-backups/`.
3. Stop everything that holds the database: `lific service stop`, and close Claude Code sessions.
4. Install the new program the way you installed the old one (section 3). With the release binary, download it and check it in a fresh folder, then **move** it over the old one with `mv`. Do not `cp` over a running program.
5. Start again: `lific service restart`, then `lific --version` and `lific doctor`.
6. Restart Claude Code.

With Homebrew: `brew upgrade lific`, then steps 5 and 6.

Going back to an older version needs both the old program and the backup you took in step 2. Anything written after the upgrade is lost.

### Remove

1. Back up first if you might want your issues later: `lific dump --out ~/lific-backups/`.
2. Disconnect Claude Code: `claude mcp remove lific -s user`.
3. Stop and remove the background service: `lific service uninstall`.
4. Delete the launcher and the key file: `rm ~/.local/bin/lific-mcp-agent ~/.config/lific/agent-token`.
5. Remove the program: `rm ~/.local/bin/lific` (or `brew uninstall lific`, or `cargo uninstall lific`).
6. Only if you are sure: delete `~/.config/lific/` and `~/.local/share/lific/`. This deletes every issue.
7. Remove `LIFIC_BOT_USER` from `.env` if you set it.

HEADING OS keeps working. The Lific panel in `/prime` disappears.

## 12. What this guide does not cover yet

- **The exact folders Lific uses on macOS.** Lific's help says "macOS equivalents" of the Linux folders and does not name them. This guide was not checked on a Mac.
- **The smallest role the bot needs.** HEADING OS runs its bot as `maintainer` and that works. Whether a smaller role is enough was not tested.
- **The Homebrew commands on a Mac.** `brew install voidnullable/tap/lific` is upstream's own line; `brew upgrade lific` and `brew uninstall lific` are ordinary Homebrew commands. None of them was run for this guide.
- **A Lific on another machine.** The engine supports it through `LIFIC_URL` and `LIFIC_API_KEY`, but this guide covers only Lific on your own computer.
