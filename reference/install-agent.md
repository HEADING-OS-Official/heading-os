# Install runbook for Claude

The one page Claude follows to install HEADING OS for an executive, from the one sentence on the site to the handoff.

Last Updated: 2026-11-03

This page is written for you, Claude. The executive pasted one sentence, `Install HEADING OS for me: follow https://headingos.com/install`, and that page sent you here. The installer decides everything: the questions, their order, the windows and the finish. You run it, relay what it says, and do nothing it did not ask for.

## The five rules

1. Run the one command below that gets the installer and starts it.
2. Read the `HEADING-INSTALL:` line. Relay `say`. For `need-answer`, ask exactly the question given, with the options given. Then run `next`. When `say` holds the install plan, relay it whole, never shortened: it is what the executive agrees to.
3. Never send the executive to a terminal unless the line's `say` does so. Only two cases do: the computer password on WSL, and a machine with no desktop. Never ask for a secret in the chat. Never edit a file.
4. On `failed`, relay `say`, give the path of the install report, `~/HeadingOS/install-report.txt`, and stop. The report holds the step that failed, its title, the time and the platform. It holds the four answers by name: mail (exchange, google, or none for later), GitHub backup, time zone, Claude sign-in. It ends with what the installer said. It holds no secret and no `.env` value. It never leaves the computer on its own: the executive decides whether to send it.
5. On `done`, say the closing sentence `say` gives and end. It names `~/HeadingOS/installed.txt`, the list of what was installed and how to remove each part by hand.

## The command

Get the installer from the release address (the default). Use the release tag in the address you read this page from, for example `v3.1.3`:

```bash
cd ~
curl -fsSLO https://raw.githubusercontent.com/HEADING-OS-Official/heading-os/<tag>/install.sh
```

Or from a local copy: download nothing. `<copy>` is the folder that holds this page's `reference/` folder, written as an absolute path starting with `/`. It must be a git repository with a `main` branch, because the installer clones it with `git clone --branch main`.

```bash
cd ~
cp <copy>/install.sh ~/install.sh
```

Then run exactly one of these, as a background command:

```bash
bash ~/install.sh
bash ~/install.sh --from <copy> --ref main
```

## The status line

Every run ends with one line: `HEADING-INSTALL: {"state": ..., "say": ..., "next": ...}`. `say` is in English; relay it in the executive's language.

- `need-answer`: `say` is the question and `options` the allowed answers. Before anything changes, the installer shows its plan and asks for consent per group of change: system packages, files in the home folder, downloads, background programs and timers. The options are always Yes, Yes to all steps, and No. In `next`, replace the bare word `{answer}` with the chosen option's `value`, character for character, and change nothing else. An option whose value is `{free}` means: ask for the executive's own words. Put them in single quotes, and write each single quote inside as `'"'"'`.
- `waiting-for-person`: a window or a browser page is open for the executive. Relay `say`, wait until they say they are done, then run `next` unchanged. The one exception: when they would rather do the Claude sign-in later, append ` --proxy-signin later` to `next`.
- `paused`: relay `say`, then ask whether to continue. On yes, run `next` unchanged.
- `stopped`: relay `say` and end.
- `failed`: rule 4. When `next` is empty, `say` names what to do first; offer no rerun.
- `done`: rule 5.

Run every installer command as a background command and wait for it to end, then relay its `say`. A long step such as the model download then outlasts the chat tool's time limit, and the chat stays free while it runs.

When the executive says pause or stop while a run goes on, run at once, as its own command:

```bash
uv run --quiet --python 3.11 --no-project <target>/engine/scripts/heading-install.py --control pause --target <target>
```

with `stop` in place of `pause` for stop, and `<target>` the install folder, `~/HeadingOS` unless the install named another. It ends with its own `HEADING-INSTALL:` line like every run; relay its `say`. The install itself then ends after the step it is running with `paused` or `stopped`. When the control command exits 1, or the engine folder does not exist yet, say: "This part of the install cannot be paused; it takes a few minutes. Say pause again once the numbered steps start." The progress window, on a desktop, has the same two buttons.

After the install, run .venv/bin/python scripts/heading-console.py open in ~/HeadingOS/engine to see every web interface and the guides.

### Google mail and calendar

With Google, the executive creates their own small Google sign-in app in the Google Cloud console. The engine signs in to Gmail and Google Calendar through it. Nothing is shared with anyone else. You conduct the console; the executive does every click in their own browser.

**a. The account type.** On the answer "Google", run the installer with `--mail google`. Then ask: "Is your Google account a company Google Workspace account, an address on your company's own domain, or a personal @gmail.com address?" A Workspace account takes branch f below.

**b. Who signs in.** Ask the executive to sign in to the Google Cloud console in their browser with the SAME Google account whose mail is connected. You never sign in. You never see a password. You never ask for the content of the client file.

**c. The console steps.** Google moves its controls, so describe what to look for and how to tell it worked. Never give a click path. Wait for the executive to confirm each step:

1. Accept the terms of service, if the console shows them.
2. Create a project. Any name works, for example "HEADING OS".
3. Enable three APIs: the Gmail API, the Google Calendar API and the Google Drive API. Drive is enabled now so that the later Drive backup needs no second visit. Nothing asks for Drive access in this install.
4. On the Branding page, enter an app name and a support email.
5. On the Audience page, choose External.
6. On the Data Access page, add three scopes: `gmail.readonly`, `gmail.modify` and `calendar.events`.
7. On the Clients page, create a client of type "Desktop app", never "Web application". Download its JSON file.
8. On the Audience page, publish the app, so that the status reads "In production".

Tell the executive why step 8 matters. An External app left in "Testing" loses its sign-in every 7 days. Source: Google's OAuth 2.0 overview, https://developers.google.com/identity/protocols/oauth2 (refresh token expiration).

**d. Place the file.** Ask the executive for the path of the downloaded file. It is normally in their Downloads folder. Then run the installer with that path, and always with `--mail google`, because without it the installer switches mail off:

```bash
cd ~/HeadingOS/engine
HEADING_OS_DATA="$HOME/HeadingOS/data" .venv/bin/python scripts/heading-install.py --mail google --google-client "<path>"
```

Never open, print or copy the file yourself. The installer refuses a Web application client; then go back to step 7. After the step reads `done`, ask the executive to delete the downloaded copy.

**e. Audience and sign-in.** Ask the executive to read the Audience page and tell you what it shows. Only when it says "In production", run the installer again:

```bash
HEADING_OS_DATA="$HOME/HeadingOS/data" .venv/bin/python scripts/heading-install.py --mail google --google-audience production
```

The installer then stops at the sign-in and prints a command. The executive types that command in a second terminal. In the browser they choose the same Google account. Google shows the screen "Google hasn't verified this app". This screen is expected, because they are the developer of their own app, which is Google's personal-use case. They click "Advanced", then "Go to" their app name, then allow access. Relay these steps. Never tell them the warning is a problem. When they say it finished, run the installer again with `--mail google` and check that every Google step reads `already in place`.

**f. A company Google Workspace account.** The project must belong to the company's organization: the console's project picker shows the organization name. On the Audience page, choose Internal. An Internal app has no publish step. Use `--mail google --google-audience internal` in step e.

- When Internal cannot be chosen, the project belongs to a personal account. Tell the executive. Either create the project again under the organization, or continue as External (step c).
- When the sign-in screen says that the administrator blocks the app, stop. Only the company's Google administrator can allow it, for example by marking the app as trusted. Never suggest a workaround.

**g. When something fails.** Each failure has one fix:

- A Web application client was downloaded: the installer refuses it. Create a "Desktop app" client (step 7) and place that file.
- The wrong Google account was chosen in the browser. When the executive wants to, they remove the app's access at https://myaccount.google.com/linkedapps. Source: Google Account Help, "Manage links between your Google Account & apps from other developers". Then they type `.venv/bin/python scripts/google-connect.py consent --fresh` in their own terminal.
- The sign-in was given while the app was still in Testing: publish the app (step 8), then `consent --fresh` as above.
- The doctor shows `gmail sign-in` or `calendar sign-in` red: the executive types `.venv/bin/python scripts/google-connect.py consent` in their own terminal.

**h. Claude's own connectors.** Offer this once, after the doctor's Google rows are green: "Claude can also read your Gmail and Calendar directly in chat. To add that, open claude.ai, Settings, Connectors, and connect Gmail and Google Calendar." Then say only what is true:

- They work only while the executive talks to Claude, and only with a Claude subscription login.
- Mail read through them is stored with the chat on Anthropic's servers.
- They do not replace the engine's own connection.
- HEADING OS still drafts every message and every calendar event for the executive's approval.
- In a parallel work session (a yard), every connector tool except a read is refused.
