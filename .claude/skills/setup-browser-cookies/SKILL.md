---
name: setup-browser-cookies
description: "Import logged-in cookies from a real Chromium-family browser (Brave, Chrome, Chromium, Edge) into the workspace's Playwright/headless cookie store (the registry name `browser-cookies` in the data overlay), using the workspace-native scripts/utils/chromium_cookies.py reader. Use before QA-testing or scraping pages that require an authenticated session. For the browser automation itself use /playwright."
argument-hint: "[domain] (omit to be prompted for the domains to import)"
allowed-tools: "Bash(.venv/bin/python:*), Read, Write"
model: haiku
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "2.0"
x-heading-capability:
  what: >
    Imports logged-in cookies from a real Chromium-family browser (Brave, Chrome,
    Chromium, Edge) into the workspace cookie store, which the data overlay answers for
    under the registry name `browser-cookies`, so Playwright and the headless browse
    session reuse the authenticated session.
  how: >
    Type /setup-browser-cookies <domain> (or omit the domain to be prompted). It
    runs scripts/utils/chromium_cookies.py to decrypt that domain's cookies from
    the local browser profile and writes a Playwright-compatible cookies.json.
  when: >
    Use before QA-testing or scraping pages that require an authenticated session.
    For the browser automation itself use /playwright.
x-heading-routing:
  category: Operations
  triggers:
    - setup browser cookies
    - import cookies
  exclusions:
    - N/A
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

# Setup Browser Cookies

Import logged-in sessions from your real Chromium-family browser into the workspace
cookie store, `browser-cookies`. That is the `cache` kind of the
`browser` service in the runtime store, and `chromium_cookies.py --store` resolves it. Backed by the workspace-native
reader `scripts/utils/chromium_cookies.py` (the same decryptor `scripts/linkedin-activity.py` uses; `/playwright` reads the store it fills) —
no external binaries.

## How it works

`scripts/utils/chromium_cookies.py` reads and decrypts cookies for a domain directly
from the browser's profile DB, per profile and per browser. The decryption backend is
DPAPI on Windows, libsecret on Linux, and Keychain on macOS. This skill drives it per
domain and assembles a Playwright-compatible `cookies.json` in the runtime address `runtime:browser/cache`.

## Steps

### 1. Determine domain(s) and browser

If the user gave a domain (e.g. `/setup-browser-cookies github.com`), use it. Otherwise
ask: **"Which domain(s) should I import cookies for, and from which browser (brave /
chrome / chromium / edge, default brave)?"** Then STOP and wait.

The profile defaults to `ClaudeCode`; pass `--profile "<name>"` if the user logs in
under a different Chromium profile.

### 2. Import the cookies straight into the store

```bash
.venv/bin/python scripts/utils/chromium_cookies.py "<domain>" --browser brave --profile ClaudeCode --store
```

One command extracts, converts to Playwright objects, and merges into the store,
keeping every other domain already there. It prints a count and nothing else.
`--store` resolves the cookie-store path itself through the data-root seam, so
this command names no path.

**Never pass `--values` here.** It prints live session tokens to stdout, and under
an agent stdout IS the transcript — which the NEVER list below forbids. `--values`
exists for a human at a private terminal, not for this flow.

If it errors:

- **`No cookies found`** (exit 1) — the read matched nothing at all. The profile is not
  logged in to that domain. Ask the user to log in in that browser/profile first.
  Read this ONLY as an empty profile. The next entry covers a read that found
  cookies and could not read them.
- **`N of M cookie(s) could not be decrypted`** (exit 4) — the cookies are there and
  unreadable. The reader leaves the store untouched on purpose. A partial import
  replaces a working session with an incomplete one. The printed `cause:` lines
  say which of the cases below applies.
- **`App-bound v20 ... not yet supported`** — Chrome M127+ app-bound encryption. Fall
  back to `yt-dlp --cookies-from-browser brave` for that workflow (documented in
  `reference/vpn-preflight.md`).
- **`decrypted bytes are not a valid cookie value`** — the key is wrong for this
  profile, or the browser changed its storage format again. Do not retry blindly;
  report it.
- **`secretstorage not installed` / locked keyring (Linux)** — only v10 cookies decrypt;
  unlock the keyring (gnome-keyring / kwallet) for v11.

### 3. Repeat per domain

Step 2 already wrote and merged the store, so there is nothing to assemble by
hand. Run it once per domain the user named; each run keeps the other domains.

Do NOT rebuild the file with the Write tool. That would mean reading every cookie
value into context to write it back out — the same leak step 2 exists to avoid.
The shape the script writes, for reference only:

```json
{"name": "<name>", "value": "<value>", "domain": "<the host the browser stored it on>", "path": "/", "secure": true, "httpOnly": false, "sameSite": "Lax"}
```

The `domain` field carries the host the browser scoped the cookie to. A domain
cookie keeps its leading dot, as in `.example.com`. A host-only cookie has no
dot, as in `accounts.example.com`. Keep that distinction: a dot added to a
host-only cookie hands its token to every subdomain.

### 4. Confirm

Tell the user: **"Imported N cookie(s) for <domain> into the runtime cookie store.
Playwright will auto-load them for future browser commands."** Report only domain
names and counts, never cookie values.

## NEVER

- NEVER print cookie values to the chat or to any log — they are live session tokens.
  Use them only to build the runtime cookie store (the runtime store's `cache` and `tmp` kinds are gitignored in the data overlay).
- NEVER commit the cookie store or paste its contents anywhere.
- NEVER send cookie data to any external service.

## Notes

- The cookie store is a `cache` kind of the runtime store; the data overlay gitignores every service's `cache` kind (`scripts/create-data-repo.py`).
- On Linux only Brave / Chrome / Chromium / Edge are supported (Comet and Arc are not
  available there).
- First read per browser may trigger a credential-vault prompt (macOS Keychain "Allow",
  Linux Secret Service unlock; Windows DPAPI is silent).
