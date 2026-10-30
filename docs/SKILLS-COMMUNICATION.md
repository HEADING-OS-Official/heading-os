# Skills: Communication and contacts

These skills write email in your voice, read and write Telegram and Mattermost as your own account, and keep your contacts: the HEADING OS CRM and, separately, Google Contacts. None of them sends anything on its own. Writing skills produce a draft; the chat clients send only after you ask for that send in your own words. For the full list of skills and how a skill is called, see `SKILLS.md`.

## /email-draft

Drafts a new email from you to a named person. It reads your voice guide, a few of your recent sent emails as style examples, your people index and your company overview, then writes a short email that states its reason in the first sentence, puts the most important point first and ends with one clear next step. Below the draft you get 2 or 3 subject line options, any relationship notes it found, and one thing to check before sending. The draft appears in the chat and is not saved or sent.

**How to call it.** `/email-draft <to> <purpose>`. Or say it in your own words, for example: "draft email to Dana Whitfield about the pilot timeline", "email Tom at Northwind about the delayed shipment". You can also give key points, a tone (`formal`, `warm`, `direct` or `urgent`; the default is warm-direct) and any background.

**Example.**

```text
You:        /email-draft Dana Whitfield, COO at Acme Corp - confirm the pilot starts on 3 March
Assistant:  Dana, writing to lock in 3 March as the pilot start. Two things need to land
            before then: ... Would Tuesday at 10:00 work for a 20 minute kickoff?
            Subject options: 1) Pilot start: 3 March  2) Locking the pilot date  3) ...
            Flag: you have not confirmed the pilot scope in writing yet.
```

**What it needs.** Your voice guide, your people index and your company overview in the data repository. Style examples come from the sent-mail cache that the Exchange mail sync (`scripts/sync-exchange.py`) keeps; this needs `EXCHANGE_EMAIL`, `EXCHANGE_PASSWORD` and `EXCHANGE_SERVER` in `.env`. If the cache is missing, the skill falls back to the voice guide alone and says so. To send the approved text yourself, `scripts/send-email.py` sends through Exchange; HEADING OS refuses that command unless your newest typed message asks for the send.

**When not to use it.** To answer an email you received, use `/email-respond`. To write to another company's CEO as a peer, use `/ceo-to-ceo`. To follow up after a meeting, use `/follow-up`.

## /email-respond

Drafts a reply to an email you received. You paste the email; the skill marks it as pasted text, works out what the sender actually wants and in what tone, checks your people index and your pipeline for the sender and any open deal, then writes a reply that answers the real question first and is no longer than the incoming email unless it has to be. You get the reply, a subject line when the thread needs one, and one thing to check before sending. Anything inside the pasted email that reads like an instruction is quoted back to you and flagged, never followed. Nothing is sent.

**How to call it.** `/email-respond <notes>`, with the incoming email pasted below your notes. The notes are optional: points to make, things to avoid, or context the email does not show. Or say it in your own words, for example: "reply to this", "draft reply, keep it short and say no to the discount".

**Example.**

```text
You:        /email-respond decline politely, offer a call next week
            [pasted email from Tom Becker, Northwind Logistics, asking for a 20% discount]
Assistant:  Tom, it sounds like the budget for this quarter got tighter than planned. ...
            Flag: his email mentions a competing quote; you may want to ask which one.
```

**What it needs.** Your voice guide, people index and pipeline in the data repository. The same optional sent-mail cache as `/email-draft` supplies style examples.

**When not to use it.** For a new email that is not a reply, use `/email-draft`. For a follow-up after a meeting, use `/follow-up`. For a high-stakes negotiation reply where you want a full playbook first, use `/voss`.

## /follow-up

Drafts a follow-up email after a meeting or event, for one person at a time. It reads your voice guide, people index, pipeline and company overview, then writes an email under 150 words: it opens with a real detail from the conversation, restates their concern so they would say "that's right", offers one piece of value, and confirms the agreed next step with a date. It asks rather than invents when the details are thin. You get the draft, a subject line and any updates your people index or pipeline should get. After drafting, it logs the interaction (date, type Email, short summary) on the person's CRM card and sets its `last_touch` to today; if there is no card, it asks whether to create one with `/crm add`. The email itself is not sent.

**How to call it.** `/follow-up <who> <context>`. Or say it in your own words, for example: "follow up with Dana Whitfield after yesterday's demo", "follow-up email after the logistics summit to Tom Becker".

**Example.**

```text
You:        /follow-up Dana Whitfield, Acme Corp - met at the logistics summit, she asked about
            rollout in two warehouses, I promised a technical overview by Thursday
Assistant:  Dana, your point about the night shift in the second warehouse stayed with me. ...
            I'll send the technical overview by Thursday.
            Subject: Two-warehouse rollout: overview coming Thursday
            Logged on Dana Whitfield's CRM card (Email, today).
```

**What it needs.** Your voice guide, people index, pipeline, company overview and personal details (for the sign-off) in the data repository, and your CRM contact cards. The optional sent-mail cache supplies style examples.

**When not to use it.** For a first email to someone, use `/email-draft`. For a reply inside a thread they started, use `/email-respond`. For a full recap of an event with many contacts, use `/event-debrief`.

## /ceo-to-ceo

Drafts a letter from you to another company's CEO or a senior executive, written as one peer to another. It reads your voice guide, the people record for that executive, your company overview and your pipeline; when the letter quotes facts or numbers, it checks them against your stored documents. The draft is 150 to 300 words, gets to the point in the first paragraph, proposes one concrete next step and ends decisively. You get the draft, two subject line options and one thing that needs extra care given the relationship. It asks rather than invent shared history. Nothing is sent.

**How to call it.** `/ceo-to-ceo <name> <subject>`. Or say it in your own words, for example: "write to Maria Lindqvist, CEO of Northwind Logistics, about expanding the partnership", "CEO letter to the Acme founder after the board meeting". You can add the objective, background and a tone (`formal`, `warm-peer` or `urgent`; the default is warm-peer).

**Example.**

```text
You:        /ceo-to-ceo Maria Lindqvist, Northwind Logistics - propose a joint pilot in Q3
Assistant:  Maria, our teams have now run three exchanges on routing data, and the pattern
            is clear enough to test. ... Would a 30 minute call with both COOs on 14 May work?
            Subject options: 1) A Q3 pilot, together  2) Next step after the routing work
            Flag: her team raised pricing twice; this draft leaves price out on purpose.
```

**What it needs.** Your voice guide, people index, company overview, pipeline and document index in the data repository. The optional sent-mail cache supplies style examples.

**When not to use it.** For a recipient who is not a CEO or senior executive, use `/email-draft`. For a reply in an existing thread, use `/email-respond`. To prepare the meeting itself, use `/meeting-prep`.

## /telegram

Reads and writes Telegram as your own personal account, not as a bot. It can list your chats, show unread ones, read a conversation, search history, send a message, reply to or forward a message, send a file, download media, show who a user or group is, list your Telegram contacts, mark a chat read and delete a message. Chats are named by `@username`, `+phone`, a numeric chat id, `me` for your Saved Messages, or a display name, which it fuzzy-matches. Downloaded media goes to the data repository under the registry name `telegram-downloads`. Messages you read are treated as untrusted: an instruction inside one is reported to you, not followed.

**How to call it.** `/telegram <send|read|search> <contact> <message>`. Or say it in your own words, for example: "check telegram", "read telegram from Dana", "send telegram to @tbecker that I'm 10 minutes late". Before sending to anyone but your Saved Messages, the assistant shows you the text and waits. HEADING OS also refuses `send`, `send-file`, `reply`, `forward` and `delete` unless your newest typed message asks for it.

**Example.**

```text
You:        what's new on telegram?
Assistant:  3 chats with unread messages:
            Dana Whitfield (2): "Can we move the call to 4pm?"
            Acme pilot group (5): ...
            Want me to draft a reply to Dana?
```

**What it needs.** The optional Telegram package: `uv sync --extra telegram`. In `.env`: `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` and `TELEGRAM_PHONE`, from https://my.telegram.org. Sign in once: `.venv/bin/python .claude/skills/telegram/scripts/telegram_client.py setup` asks Telegram for a code, then `... verify <code>` completes it. If your account has two-step verification, put the password in `.env` as `TELEGRAM_2FA_PASSWORD`, or run `verify` in a terminal for a hidden prompt; a password on the command line is refused. The session is kept, gitignored, under `.sessions/telegram/`. Add `--json` before the command for structured output.

**When not to use it.** For Mattermost, use `/mattermost`. For email, use `/email-draft` or `/email-respond`. It is not for building Telegram bots or writing Telegram code.

## /mattermost

Reads and writes Mattermost as your own account, through the server's REST API. It sees what your account sees: your teams, channels, group and direct messages. It can show who the token belongs to (`whoami`), list channels with unread and mention counts, show only unread ones, read the last messages of a channel or direct message, read a whole thread, search history with Mattermost search syntax (`from:`, `in:`, `after:`, quotes), find people, post a message, reply in a thread and mark a channel read. Targets are `@username`, `~channel-name`, a channel display name, or a 26-character channel id; an ambiguous name lists the candidates and stops. What it reads is not saved anywhere.

**How to call it.** `/mattermost <unread|read|search|send> <target> <text>`. Or say it in your own words, for example: "check mattermost", "read mattermost ~release-planning", "send on mattermost to @dana that the build is green". Sending works in two steps: the assistant shows the exact text and target and stops; it sends only after you reply with a word that asks for the send, such as "send". "ok", "yes" or "go" alone do not count. HEADING OS refuses `send` and `reply` otherwise, and a refusal means "show me the draft again".

**Example.**

```text
You:        /mattermost unread
Assistant:  ~release-planning: 4 unread, 1 mention
            @dana: 2 unread
            The mention is from Tom Becker: "Can someone confirm the Friday cut-off?"
            Want me to draft a reply?
```

**What it needs.** In `.env`: `MATTERMOST_URL` (your server, `https://...`) and `MATTERMOST_TOKEN` (a personal access token of your own account). Without either, every command stops with exit code 2 and names them. No extra package is needed. Long messages are passed with `--body-file` from your scratch folder (`.venv/bin/python scripts/data-path.py scratch`).

**When not to use it.** For Telegram, use `/telegram`. For email, use `/email-draft` or `/email-respond`. It is not for administering the Mattermost server or exporting its history.

## /crm

Your personal CRM: one contact card per person in the data repository (registry name `crm-contacts`), each with a profile, open commitments and a dated interaction log. The first word picks the action. `radar`, the default, runs `scripts/crm-health.py` and lists contacts by urgency: RED (overdue), YELLOW (due soon), commitments due within 7 days, then GREEN; on request, `scripts/generate-crm-dashboard.py` builds a full HTML dashboard. `next` runs `scripts/crm_next.py`, which saves today's top 3 follow-ups with drafts under the registry name `crm-operations`; you decide per draft. `add` creates a contact and adds it to your people index, and always asks where the person lives so the card gets a timezone. `log` adds an interaction at the top of the log and sets `last_touch` to today. `find` searches names, companies, regions, types and history. `update` changes one field. `backfill` reconciles cards with your Exchange mail.

**How to call it.** `/crm <add|log|radar|find|update|next|backfill> <contact> <details>`. Or say it in your own words, for example: "check CRM", "contact health", "crm log Dana Whitfield, call today, agreed to send the contract Friday".

**Example.**

```text
You:        /crm radar
Assistant:  RED (2): Tom Becker, Northwind Logistics - 41 days, cadence 30. Suggest a short call.
            YELLOW (3): Dana Whitfield, Acme Corp - due in 4 days ...
            Commitments due this week: contract draft to Dana (Friday)
            Run /crm next to see the top 3 follow-ups with drafts ready for review.
```

**What it needs.** The CRM config and your people index in the data repository; the pipeline when you log deal interactions. `next` sends nothing: an approved draft is sent with `scripts/send-email.py`, which needs the Exchange keys in `.env` and your typed word for the send. `backfill` needs the optional email package (`uv sync --extra email`) and the Exchange keys. It runs in two steps: `scripts/sync-exchange.py --export-metadata <file> --since YYYY-MM-DD` exports mail headers (no bodies), then `scripts/crm-backfill.py --mail <file>` reports. It writes only with `--apply` and the `--approved` list you confirmed.

**When not to use it.** For your Google address book, use `/google-contacts`. To draft one follow-up email after a meeting, use `/follow-up`. To recall what was said about someone across all records, use `/recall`.

## /google-contacts

Searches, adds, edits, lists, shows and deletes contacts in your Google Contacts, through the Google People API. It is a separate connector: it does not read or write the HEADING OS CRM. The assistant searches first, opens the full record to get its resource name (it looks like `people/c1234567890`), and adds a new contact only when none exists. Search matches the start of names, emails and phone numbers and returns up to 30 results by default. Deleting is permanent.

**How to call it.** `/google-contacts <search|add|edit> <name>`. Only the typed command starts it; plain words do not. Under the hood it runs `scripts/google-contacts.py` with `search`, `add`, `get`, `edit`, `list` or `delete`. `add` takes `--name` (required), `--email`, `--phone`, `--company`, `--title`, `--notes`, `--address` and `--url`; `list --limit N` defaults to 100; every command accepts `--json`.

**Example.**

```text
You:        /google-contacts add Dana Whitfield, COO, Acme Corp, dana@example.com
Assistant:  No existing contact matches "Dana Whitfield". Added:
            Dana Whitfield, COO, Acme Corp, dana@example.com (people/c4821...)
```

**What it needs.** The Google client libraries, installed by the standard setup (`uv sync --all-extras --group dev`). An OAuth client of type "Desktop application" from the Google Cloud Console, with the People API enabled, saved as `.sessions/google/credentials.json`, or at the path in `GOOGLE_CONTACTS_CREDENTIALS_PATH` in `.env`. There is no API key. The first command opens a browser for Google's consent screen; the token is then kept in `.sessions/google/token.json` and refreshed by itself. If sign-in breaks, delete that token file and run a command again.

**When not to use it.** For relationship tracking, interaction logs and follow-up health, use `/crm`. It does not handle Exchange or Outlook contacts.
