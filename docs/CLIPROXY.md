# CLIProxyAPI: one door to other AI models

This guide shows you how to install CLIProxyAPI, set it up, sign in to each AI provider HEADING OS uses, connect it to the engine, and use it every day. CLIProxyAPI is optional. The core of HEADING OS runs without it. Without it you lose the features that ask models other than Claude: second opinions, deep research checks and branch reviews.

A few words you will see below:

- **Proxy** means a program that sits between two others and passes messages along. Here it sits between the engine and the AI providers.
- **API** means the way one program asks another for something. "OpenAI-compatible API" means the proxy answers in the same format as OpenAI's own service, so one client works for every provider behind it.
- **OAuth login** means you sign in through a web page in your browser, and the program keeps a token (a long-lived pass) instead of your password.
- **The engine** is the HEADING OS code folder, `<parent>/.heading-os`. Its settings file is `<parent>/.heading-os/.env`. The default `<parent>` is `~/ai/claude-workspaces`. See `INSTALL.md`.
- **Yard** means a separate git checkout of the engine for one task (see `YARDS.md`). The main engine folder is called the main clone.

Run engine commands from the engine folder:

```bash
cd <parent>/.heading-os
```

---

## 1. What it is and why HEADING OS uses it

CLIProxyAPI "is a proxy server that provides OpenAI/Gemini/Claude/Codex/Grok compatible API interfaces for CLI" (source: https://github.com/router-for-me/CLIProxyAPI, MIT license). In plain words: you sign in to your AI subscriptions once, inside the proxy. After that, any program on your machine can use those subscriptions through one local address and one key.

HEADING OS uses it for every call to a model that is not Claude. All such calls go through one function, `call_model` in `scripts/utils/proxy_transport.py`. That function always talks to this fixed address:

```
http://127.0.0.1:8317/v1
```

The address is written in the code (`PROXY_BASE_URL`). No setting changes it. So your proxy must listen on `127.0.0.1`, port `8317`.

What uses the proxy:

| Feature | What it does | Model it asks for |
|---|---|---|
| `/council` skill | Asks other models for a second opinion beside Claude's own. By default it asks Kimi only; `--gemini`, `--grok`, `--chatgpt` or `--all` add more voices. | Kimi: `k3.1-pro` (written in the skill itself). The others: the pins in `config/council-models.json`. |
| `scripts/kimi-consult.py`, `scripts/chatgpt-consult.py`, `scripts/gemini-consult.py`, `scripts/grok-consult.py` | The four "voices" of the council. You can also run them yourself. | The pins in `config/council-models.json`, unless you pass `--model`. |
| `/scrutinize` | Its judge voices. | The pins `kimi_reasoning` and `chatgpt_reasoning`. |
| `/deep-research-advance` | Checks web research (fetched with Perplexity) by reasoning over it. | `k3`, or `kimi-for-coding` when the proxy does not offer `k3`. |
| `scripts/review-branch.py` | Reviews a yard's branch before it is merged. | `gpt-6-astra` by default (configuration `astra-rules`); `--config k3-rules` uses `k3`. |
| `scripts/hindsight-sync.py` | Part of the memory server sync (see `HINDSIGHT.md`). | `kimi-for-coding` |
| `scripts/utils/llm_fallback.py` | A fallback when a Claude API call fails. | `kimi-for-coding`, then `gpt-6-astra` (from `config/llm_fallback.yaml`). |

Every call through `call_model` also writes one line of token counts to a usage log in your data repository. `AGENTVIEW.md` shows how to read it.

> **Warning: privacy.** These models run in other companies' clouds. Whatever you send leaves your machine. Never send private data through `/council`, the consult scripts or `/deep-research-advance`.

---

## 2. What you need first

- Linux, WSL2 on Windows, or macOS.
- `curl` and `tar` (both are present on almost every system).
- An account with each provider you want to use:
  - **ChatGPT** (OpenAI) for `gpt-6-astra`. The proxy signs in through OpenAI's Codex login.
  - **Kimi** (Moonshot) for `kimi-for-coding`, `k3` and `k3.1-pro`. You need a Kimi plan that includes "Kimi for coding".
  - **Gemini** (Google) for `gemini-3.8-flash-high`. The proxy signs in through Google Antigravity.
  - **Grok** (xAI) for `grok-4.6`.
- The HEADING OS engine installed (see `INSTALL.md`).

You do not need all four providers. Each one you skip simply cannot answer. `/council` asks only Kimi unless you add other voices.

---

## 3. Install, step by step

The steps below put the program in the folder `~/cliproxyapi`. Use that folder. The engine's automatic updater (section 9) expects the program at exactly `~/cliproxyapi/cli-proxy-api`.

### Linux and WSL2

1. Open https://github.com/router-for-me/CLIProxyAPI/releases and note the newest version number, for example `8.0.44`.
2. Make the folder and go into it:

   ```bash
   mkdir -p ~/cliproxyapi && cd ~/cliproxyapi
   ```

3. Download the program and its checksum list. Replace `8.0.44` with the newest number. On an ARM computer, use `linux_aarch64` instead of `linux_amd64`.

   ```bash
   VERSION=8.0.44
   curl -fLO https://github.com/router-for-me/CLIProxyAPI/releases/download/v$VERSION/CLIProxyAPI_${VERSION}_linux_amd64.tar.gz
   curl -fLO https://github.com/router-for-me/CLIProxyAPI/releases/download/v$VERSION/checksums.txt
   ```

4. Check that the download is the real one. A checksum is a fingerprint of the file:

   ```bash
   grep "CLIProxyAPI_${VERSION}_linux_amd64.tar.gz" checksums.txt | sha256sum -c -
   ```

   It must print `CLIProxyAPI_8.0.44_linux_amd64.tar.gz: OK`. If it prints anything else, delete the file and download it again.

5. Extract it:

   ```bash
   tar -xzf CLIProxyAPI_${VERSION}_linux_amd64.tar.gz
   ```

   You now have `cli-proxy-api` (the program), `config.example.yaml` (a sample settings file), `README.md` and `LICENSE`.

6. Check that it runs:

   ```bash
   ./cli-proxy-api -help | head -1
   ```

   It prints a line such as `CLIProxyAPI Version: 8.0.44, ...`.

### macOS

Follow the same six steps, with two changes:

- In step 3, download `darwin_aarch64` (Apple silicon: M1 and newer) or `darwin_amd64` (Intel) instead of `linux_amd64`.
- In step 4, use `shasum -a 256 -c -` instead of `sha256sum -c -`.

The upstream quick start (https://help.router-for.me/introduction/quick-start.html) also lists a Homebrew install, `brew install cliproxyapi`. This guide did not check that route. If you use it, the program and its settings file live where Homebrew puts them, and the engine's automatic updater will not work for you; update with `brew upgrade` instead.

---

## 4. First-time setup

### 4.1 Make your settings file

1. Copy the sample:

   ```bash
   cd ~/cliproxyapi
   cp config.example.yaml config.yaml
   chmod 600 config.yaml
   ```

   `chmod 600` makes the file readable only by you. It will hold keys.

2. Make a key for the engine. This is a password you invent; it lets local programs use the proxy. This command prints a strong random one:

   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

   Copy the output somewhere safe for the next steps. We call it `<your-proxy-key>`.

3. Open `config.yaml` in a text editor and change three places. The file is long and full of comments; leave everything else as it is.

   **a. Listen only on your own machine.** Find the `server:` section. Change `host: ""` to:

   ```yaml
   server:
     host: "127.0.0.1"
     port: 8317
   ```

   This matters. The sample's empty `host` means "every network interface", so other computers on your network could reach the proxy. `127.0.0.1` means only this machine. Keep the port at `8317`; the engine uses no other.

   **b. Set the key.** Find the `access:` section. Replace the three sample keys with yours:

   ```yaml
   access:
     api-keys:
       - "<your-proxy-key>"
   ```

   **c. Check where logins are stored.** Find the `oauth:` section. It has `auth-dir: "~/.cli-proxy-api"`. Leave it. Each provider login saves a token file in that folder.

4. Leave `management:` → `secret-key: ""` empty unless you want the web control panel. Empty turns the management interface off, which is the safest choice.

> **Warning:** `config.yaml` and the folder `~/.cli-proxy-api` hold keys and login tokens. Keep both outside every git repository. Never paste them into a chat, an issue or a document.

### 4.2 Run it once in the foreground

"Foreground" means it runs in this terminal window and stops when you close it or press Ctrl+C.

```bash
~/cliproxyapi/cli-proxy-api -config ~/cliproxyapi/config.yaml
```

Leave it running and open a second terminal for the next steps. Later, in section 4.4, you make it start by itself.

### 4.3 Sign in to each provider

Each login is the same program with a login flag. It saves a token in `~/.cli-proxy-api` and then exits. Always pass your settings file, so the token lands in the right folder.

On WSL2 or a machine without a desktop, add `-no-browser`. The program then prints a web address instead of opening a browser; copy it into any browser, including the Windows one, and sign in there.

**ChatGPT (for `gpt-6-astra`):**

```bash
~/cliproxyapi/cli-proxy-api -config ~/cliproxyapi/config.yaml -codex-login -no-browser
```

If the browser cannot return to your machine after you sign in, use the device-code login instead. It shows a short code that you type on OpenAI's page:

```bash
~/cliproxyapi/cli-proxy-api -config ~/cliproxyapi/config.yaml -codex-device-login
```

**Grok (xAI):**

```bash
~/cliproxyapi/cli-proxy-api -config ~/cliproxyapi/config.yaml -xai-login -no-browser
```

**Gemini (through Google Antigravity):**

```bash
~/cliproxyapi/cli-proxy-api -config ~/cliproxyapi/config.yaml -antigravity-login -no-browser
```

**Kimi.** Kimi works in a different way. The model names the engine uses (`kimi-for-coding`, `k3`, `k3.1-pro`) come from Kimi's coding service, which takes an API key rather than a browser login. On the setup this guide was checked against, Kimi is connected with such a key:

1. In your Kimi account, open the settings of your "Kimi for coding" plan and copy its API key. We call it `<your-kimi-key>`.
2. In `config.yaml`, find the line `# END API KEY EXAMPLES`. It closes a long block of commented examples. Directly below it, add this section. Indentation (the spaces at the start of each line) must be exactly as shown:

   ```yaml
   api-keys:
     claude:
       - name: kimi
         base-url: "https://api.kimi.com/coding"
         headers:
           User-Agent: "KimiCLI/<version>"
         keys:
           - api-key: "<your-kimi-key>"
         models:
           - name: "kimi-for-coding"
             alias: "kimi-for-coding"
           - name: "k3"
             alias: "k3"
           - name: "k3.1-pro"
             alias: "k3.1-pro"
   ```

   Why `claude`? Kimi's coding service speaks the same language as Anthropic's API, so the proxy treats it like a Claude endpoint at another address. Why the `User-Agent` line? Kimi's coding service only answers programs that identify as a coding agent; on the checked setup the value starts with `KimiCLI/` followed by a version number.
3. Stop the proxy (Ctrl+C in its window) and start it again (section 4.2), so it reads the new section.

Two things about Kimi are not verified by this guide, and you should know them:

- The block above follows the layout of `config.example.yaml` in CLIProxyAPI 8.0.44. The setup it was checked against uses the older layout of the same settings (a top-level `claude-api-key:` list), which this version still accepts.
- The proxy also has two Kimi browser logins, `-kimi-login` (kimi.com) and `-kimi-ai-login` (Kimi.ai). Not documented yet: which model names they offer, and whether they include the three names the engine uses.

**Claude.** The proxy has `-claude-login`, but HEADING OS talks to Claude through Claude Code itself. You do not need it for anything in this guide.

### 4.4 Make it start by itself (Linux and WSL2)

The engine does not ship a service file for the proxy. You write one. It is a "systemd user service": a small text file that tells Linux to keep the program running for your user. The name `cliproxyapi.service` matters, because the engine's updater restarts exactly that name.

1. Stop the foreground copy from section 4.2 (Ctrl+C).
2. Create the file `~/.config/systemd/user/cliproxyapi.service` with this content. `%h` means your home folder; systemd fills it in.

   ```ini
   [Unit]
   Description=CLIProxyAPI local model proxy
   After=network-online.target
   Wants=network-online.target

   [Service]
   Type=simple
   WorkingDirectory=%h/cliproxyapi
   ExecStart=%h/cliproxyapi/cli-proxy-api -config %h/cliproxyapi/config.yaml
   Restart=on-failure
   RestartSec=3

   [Install]
   WantedBy=default.target
   ```

3. Load it, start it, and let it run after a reboot even when you are not logged in:

   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now cliproxyapi.service
   loginctl enable-linger "$USER"
   ```

On WSL2 this needs systemd. If `systemctl` says systemd is not running, add a `[boot]` section with `systemd=true` to `/etc/wsl.conf`, restart WSL, and try again.

**macOS:** there is no systemd. Run the proxy in a terminal window as in section 4.2 whenever you need it. Upstream documents `brew services start cliproxyapi` for a Homebrew install; this guide did not check it.

---

## 5. Connect it to HEADING OS

1. Open `<parent>/.heading-os/.env` in a text editor.
2. Find the line `CLIPROXY_API_KEY=` and put your proxy key after the `=` sign:

   ```bash
   CLIPROXY_API_KEY=<your-proxy-key>
   ```

   It must be one of the keys in `access.api-keys` in `config.yaml`. The engine reads no other setting for the proxy.
3. Save the file. `.env` is ignored by git, so the key is never committed.
4. Optional, for `/deep-research-advance` only: set `PERPLEXITY_API_KEY=` to a Perplexity key. Web search for that skill goes to Perplexity directly, not through the proxy.

Without `CLIPROXY_API_KEY`, every council voice stops at once with exit code 2 and this message: `CLIPROXY_API_KEY is missing from .env.`

### 5.1 The model pins

A "pin" is the exact model name a feature asks the proxy for. Every name must be one your proxy offers. Here is where each pin lives:

| Pin | Default | Where it is |
|---|---|---|
| `gemini` | `gemini-3.8-flash-high` | `config/council-models.json` |
| `grok` | `grok-4.6` | `config/council-models.json` |
| `kimi` | `kimi-for-coding` | `config/council-models.json` |
| `chatgpt` | `gpt-6-astra` | `config/council-models.json` |
| `kimi_reasoning` | `k3.1-pro` | `config/council-models.json` (the `/scrutinize` judge) |
| `chatgpt_reasoning` | `gpt-6-astra` | not in the file by default; the built-in value from `scripts/utils/council_models.py` is used |
| `/council` Kimi voice | `k3.1-pro` | written in `.claude/skills/council/SKILL.md`; if the proxy does not offer it, the skill skips the Kimi voice and says so |
| Branch review | `gpt-6-astra`, or `k3` | `scripts/utils/diff_review.py` |
| Deep research reasoning | `k3`, else `kimi-for-coding` | `scripts/deep-research-advance.py` |
| Memory sync | `kimi-for-coding` | `scripts/hindsight-sync.py` |
| Claude fallback | `kimi-for-coding`, `gpt-6-astra` | `config/llm_fallback.yaml` |

If `config/council-models.json` is missing or broken, each council pin falls back to its built-in value, so `/council` never breaks on a bad file.

Change the council pins with the pin tool rather than by hand:

```bash
.venv/bin/python scripts/council-models.py --show                    # print every pin
.venv/bin/python scripts/council-models.py --get kimi                 # print one pin
.venv/bin/python scripts/council-models.py --set grok=<model-name>    # change one pin
.venv/bin/python scripts/council-models.py --set gemini=<a> chatgpt=<b>   # change two
.venv/bin/python scripts/council-models.py --check                    # is each pin offered by the proxy?
```

`--check` asks the proxy for its list of models and reports each council pin as `ok`, `broken` (the proxy does not offer it) or `unknown` (the proxy did not answer). It only reads; it changes nothing. `--check --quiet` prints one line, empty when all is well. `--check --json` prints the result for other programs. An unknown pin name in `--set` or `--get` exits with code 2.

### 5.2 See which models your proxy offers

This prints every model name your proxy offers. It sends your key through standard input, so the key never appears in the list of running programs:

```bash
KEY="$(sed -n 's/^CLIPROXY_API_KEY=//p' <parent>/.heading-os/.env | head -n 1)"
printf 'header = "Authorization: Bearer %s"\n' "$KEY" | curl -s -K - http://127.0.0.1:8317/v1/models | python3 -c "import json,sys; [print(m['id']) for m in json.load(sys.stdin)['data']]"
```

Every pin from section 5.1 that you want to use must appear in this list.

---

## 6. Daily use, with examples

**In a Claude Code session.** Just ask, or use the skill:

- `/council should we cache this result or recompute it each time?` asks Kimi beside Claude.
- `/council --all <your question>` asks all four voices.
- "Get a second opinion from Gemini on this plan" routes to `/council` with the Gemini voice.

**From the terminal.** Each voice script has two modes:

- `--mode independent --question "..."`: the model answers the question fresh.
- `--mode critique --draft "..."`: the model looks for weaknesses in your draft.

Examples:

```bash
.venv/bin/python scripts/kimi-consult.py --mode independent --question "What are three risks of storing settings in environment variables?"
.venv/bin/python scripts/chatgpt-consult.py --mode critique --draft "$(cat my-plan.md)" --context "A plan for a small internal tool."
.venv/bin/python scripts/kimi-consult.py --mode independent --question "..." --model k3.1-pro --reasoning-effort high --max-tokens 12000
```

Useful options, from each script's `--help`:

| Option | What it does |
|---|---|
| `--context "..."` | Extra background for either mode. |
| `--model <name>` | Use another model than the pin, for this one call. |
| `--temperature <n>` | How varied the answer is. Default 0.7 for independent, 0.4 for critique. |
| `--max-tokens <n>` | The longest answer allowed. Default 8192. |
| `--length-hint "..."` | The length request added to the prompt. Default `Aim for 200-400 words.` Pass `""` for "list every problem" tasks. |
| `--timeout <seconds>` | The wait limit for one request. Raise it, for example to `480`, for a big critique. |
| `--reasoning-effort` | Kimi: `low`, `high`, `max`. ChatGPT: `low`, `medium`, `high`, `xhigh`, `max`. Leave it out for `kimi-for-coding`. Gemini and Grok do not have it. |

Tip: put a long draft in a file and pass it with `"$(cat file)"`. Typing a long text with backticks inside double quotes on the command line makes the shell run the backtick parts.

**Exit codes** of the four voice scripts: `0` the answer is printed; `2` a wrong argument or a missing key; `3` the call failed (network, rate limit, an unknown model, or a refused key).

**How long a call can take.** One request waits up to 300 seconds by default. When the proxy answers "503 busy", the engine tries again up to four times, waiting 5, 20 and 45 seconds between tries. When an answer is cut off for length, it tries once more with a bigger budget. So a single call can take many minutes in the worst case. That is normal; do not kill it early.

---

## 7. How to check it works

Do these in order. Each one checks one layer.

1. **The program runs.** On Linux and WSL2: `systemctl --user status cliproxyapi.service` must say `active (running)`. Otherwise check that the foreground window from section 4.2 is still open.
2. **The key works.** Run the two lines from section 5.2. You must get a list of model names. If you get nothing, run the same with `-w '%{http_code}\n' -o /dev/null` added after `curl -s`: `200` is good, `401` means the key is wrong.
3. **The pins are offered:**

   ```bash
   .venv/bin/python scripts/council-models.py --check
   ```

   Every voice you plan to use must say `ok`. The last line reads `All council pins are current.` when all four are offered.
4. **One real answer:**

   ```bash
   .venv/bin/python scripts/kimi-consult.py --mode independent --question "Reply with the single word ready."
   echo "exit code: $?"
   ```

   You must see an answer and `exit code: 0`. Repeat with `chatgpt-consult.py`, `gemini-consult.py` and `grok-consult.py` for each provider you signed in to.
5. **In a session:** run `/council --kimi-only <a harmless public question>`.

---

## 8. Common problems and fixes

| What you see | Why, and what to do |
|---|---|
| `CLIPROXY_API_KEY is missing from .env` | Add the key to `<parent>/.heading-os/.env` (section 5). |
| `Proxy auth failed for <model>`, or HTTP `401` | The key in `.env` is not in `access.api-keys` in `config.yaml`, or has a typo or extra spaces. Copy it again. |
| Exit code 3 and the error names an unknown or invalid model | A pin names a model your proxy does not offer. Compare section 5.2's list with `scripts/council-models.py --show`, then fix the pin with `--set`, or sign in to the missing provider. |
| `Connection refused` | The proxy is not running, or it listens on another port. Start it (section 4.2 or 4.4) and check `server.port` is `8317`. |
| `--check` says `unknown` for every pin | The proxy did not answer at all. Same fix as `Connection refused`. |
| The proxy is reachable from other computers | `server.host` is empty. Set it to `"127.0.0.1"` and restart. |
| A login opens no browser, or the browser cannot reach back | Add `-no-browser` and open the printed address yourself. For ChatGPT, use `-codex-device-login`. If a login needs a different local port for its return trip, `-oauth-callback-port <port>` sets it. |
| Kimi answers with a refusal about the client | The `User-Agent` header in the Kimi section is missing or wrong (section 4.3). |
| A provider worked yesterday and fails today | Its login token expired or was revoked. Run that provider's login again (section 4.3), then restart the proxy. |
| A proxy `503` | The engine already retries this by itself (section 6). A provider that stays down still fails after the last try. |
| The service will not start on WSL2 | systemd is off. See the end of section 4.4. Then read the log: `journalctl --user -u cliproxyapi.service -n 50`. |

---

## 9. Updating and removing

### Update by hand (any system)

1. Download and check the new version exactly as in section 3, steps 2 to 4, with the new number. This puts the new archive beside the old program.
2. Keep a copy of the working program: `cp ~/cliproxyapi/cli-proxy-api ~/cliproxyapi/cli-proxy-api.bak`.
3. Stop the proxy: `systemctl --user stop cliproxyapi.service` (Linux and WSL2), or Ctrl+C in its window (macOS).
4. Extract only the program: `tar -xzf CLIProxyAPI_${VERSION}_linux_amd64.tar.gz cli-proxy-api` (use your file name). This replaces `cli-proxy-api` and leaves your `config.yaml` alone.
5. Start it again: `systemctl --user start cliproxyapi.service`, or section 4.2.
6. Run section 7, steps 2 and 3. If something broke, stop the proxy, copy `cli-proxy-api.bak` back over `cli-proxy-api`, and start it again.

Read the release notes before a big version jump. Version 8 changed the layout of the settings file; older files still load, but new settings use the new layout.

### Automatic updates (Linux and WSL2 on x86-64 only)

The engine has an update manager. Its list of programs, `config/update-registry.yaml`, includes CLIProxyAPI as component `cliproxyapi`, at tier `auto`: the daily run installs a new version by itself. Its updater, `scripts/updaters/cliproxyapi_update.py`:

1. finds the newest release on GitHub and refuses to go to an older version;
2. downloads the `linux_amd64` archive and `checksums.txt`, and refuses to install when the checksum is missing or does not match;
3. backs up the running program, swaps in the new one, and restarts `cliproxyapi.service`;
4. checks health once a second for up to 30 seconds, and puts the old program back when the check fails.

It works only with the layout of this guide: the program at `~/cliproxyapi/cli-proxy-api`, the service named `cliproxyapi.service`, and a health check program at `~/.local/bin/cliproxy`. The engine does **not** ship that health check. You write it. Without it, the updater refuses to swap anything.

**Write the health check.** The updater runs `~/.local/bin/cliproxy health` and expects the text `HTTP 200` in its output and exit code 0. This small script asks the proxy for its model list with your key and prints the result. Replace `<parent>` with your real parent folder.

```bash
mkdir -p ~/.local/bin
cat > ~/.local/bin/cliproxy <<'EOF'
#!/usr/bin/env bash
# Health check for the HEADING OS CLIProxyAPI updater: prints "HTTP <code>",
# exits 0 only on 200. The key is passed on standard input, never as an argument.
set -euo pipefail
KEY="$(sed -n 's/^CLIPROXY_API_KEY=//p' <parent>/.heading-os/.env | head -n 1)"
code="$(printf 'header = "Authorization: Bearer %s"\n' "$KEY" | curl -s -o /dev/null -w '%{http_code}' -K - http://127.0.0.1:8317/v1/models || true)"
echo "HTTP $code"
[ "$code" = "200" ]
EOF
chmod +x ~/.local/bin/cliproxy
```

Test it: `~/.local/bin/cliproxy health` must print `HTTP 200`. With a wrong key it prints `HTTP 401` and exits with 1, which is what makes the updater roll back.

**Turn on the daily update run.** From the main engine folder (not a yard):

```bash
bash scripts/install-update-manager-timer.sh
```

It installs a systemd user timer that runs every day at 07:00 in your `HEADING_OS_TZ` time zone (UTC if unset). It runs `scripts/update-manager.py check`, then `scripts/update-manager.py apply --auto`. You can also run the update manager by hand, from the main engine folder: `.venv/bin/python scripts/update-manager.py status`, `.venv/bin/python scripts/update-manager.py check`, or `.venv/bin/python scripts/update-manager.py apply cliproxyapi`. It refuses to run inside a yard.

The updater's exit codes: `0` already current, or the new version is healthy; `1` the swap or the health check failed and it rolled back; `2` it could not find, download or stage a release, and nothing was changed; `3` the checksum was missing or wrong, and nothing was changed.

On macOS, on ARM Linux, or with a Homebrew install, the automatic updater does not apply. Update by hand.

### Remove it

1. Stop it and turn off the service (Linux and WSL2):

   ```bash
   systemctl --user disable --now cliproxyapi.service
   rm ~/.config/systemd/user/cliproxyapi.service
   systemctl --user daemon-reload
   ```

2. Delete the program folder: `rm -r ~/cliproxyapi`. This deletes `config.yaml` with your keys.
3. Delete the login tokens: `rm -r ~/.cli-proxy-api`. Also sign out of the proxy's sessions on each provider's website if you want them fully revoked.
4. If you made the health check, delete it: `rm ~/.local/bin/cliproxy`.
5. In `<parent>/.heading-os/.env`, clear `CLIPROXY_API_KEY=`.

After that, `/council` and the consult scripts stop with exit code 2, and the rest of HEADING OS keeps working.

---

## What this guide does not cover yet

- Which model names the Kimi browser logins (`-kimi-login`, `-kimi-ai-login`) offer.
- The Homebrew install and `brew services` route on macOS; the guide lists them from the upstream documentation but did not run them.
- A ready-made service for macOS. Run the proxy in a terminal there.
- The many other settings in `config.yaml` (routing between several accounts, request retries, logging). Read the comments in `config.example.yaml` and the upstream guides at https://help.router-for.me/.
