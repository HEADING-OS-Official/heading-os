# Telegram Client - Command Reference

Consumed by: `.claude/skills/telegram/SKILL.md` (command syntax reference).

Last Updated: 2026-10-16

Base command: `.venv/bin/python ".claude/skills/telegram/scripts/telegram_client.py"`

Global flag: `--json` — output structured JSON instead of colored text.

---

## setup

First-time authentication, step 1 of 2. Requests a verification code from Telegram and exits; it does not prompt. Run `verify` with the code next.

```bash
.venv/bin/python ".claude/skills/telegram/scripts/telegram_client.py" setup
```

Only needed once. The session persists under `.sessions/telegram/` in the engine clone (gitignored).

---

## verify

First-time authentication, step 2 of 2. Signs in with the code `setup` requested.

```bash
.venv/bin/python ".claude/skills/telegram/scripts/telegram_client.py" verify <code>
```

| Arg | Required | Description |
|-----|----------|-------------|
| `code` | Yes | The verification code from Telegram |

When the account has two-step verification, set `TELEGRAM_2FA_PASSWORD` in `.env`. The `--password` flag is refused, so the password never reaches the command line.

---

## send

Send a text message.

```bash
.venv/bin/python ".claude/skills/telegram/scripts/telegram_client.py" send <chat> "message text"
```

| Arg | Required | Description |
|-----|----------|-------------|
| `chat` | Yes | @username, +phone, chat ID, display name, or `me` |
| `message` | Yes | Message text (quote if spaces) |

Examples:
```bash
send me "Note to self"
send "@johndoe" "Are we still on for tomorrow?"
send "John Smith" "Meeting confirmed"
send "+1234567890" "Hey, it's me"
```

---

## read

Read recent messages from a chat.

```bash
.venv/bin/python ".claude/skills/telegram/scripts/telegram_client.py" read <chat> [--limit N] [--min-id ID] [--reverse]
```

| Arg | Required | Default | Description |
|-----|----------|---------|-------------|
| `chat` | Yes | - | Chat identifier |
| `--limit` / `-n` | No | 20 | Number of messages |
| `--min-id` | No | - | Only messages with ID greater than this (server-side filter) |
| `--reverse` | No | off | Chronological order, oldest first |

Output includes: message ID, timestamp, sender name, text, media indicator, reply reference.

---

## chats

List conversations/dialogs.

```bash
.venv/bin/python ".claude/skills/telegram/scripts/telegram_client.py" chats [--limit N] [--unread]
```

| Arg | Required | Default | Description |
|-----|----------|---------|-------------|
| `--limit` / `-n` | No | 30 | Number of chats |
| `--unread` / `-u` | No | false | Only show chats with unread messages |

---

## unread

Shortcut for `chats --unread`. Shows only chats with unread messages.

```bash
.venv/bin/python ".claude/skills/telegram/scripts/telegram_client.py" unread [--limit N]
```

---

## search

Search messages globally or within a specific chat.

```bash
.venv/bin/python ".claude/skills/telegram/scripts/telegram_client.py" search "query" [--chat X] [--limit N]
```

| Arg | Required | Default | Description |
|-----|----------|---------|-------------|
| `query` | Yes | - | Search text |
| `--chat` / `-c` | No | all chats | Limit search to specific chat |
| `--limit` / `-n` | No | 20 | Max results |

---

## send-file

Send a file, photo, or document.

```bash
.venv/bin/python ".claude/skills/telegram/scripts/telegram_client.py" send-file <chat> <path> [--caption text]
```

| Arg | Required | Description |
|-----|----------|-------------|
| `chat` | Yes | Chat identifier |
| `path` | Yes | File path: a relative path resolves under the data root (`..` out of it is refused); an absolute path is sent as given |
| `--caption` | No | Caption text for the file |

---

## download

Download media from a message.

```bash
.venv/bin/python ".claude/skills/telegram/scripts/telegram_client.py" download <chat> <msg_id> [--output path]
```

| Arg | Required | Default | Description |
|-----|----------|---------|-------------|
| `chat` | Yes | - | Chat identifier |
| `msg_id` | Yes | - | Message ID containing media |
| `--output` / `-o` | No | the overlay's downloads directory | Output directory. The default is resolved in code, not spelled here: `telegram_client.py` asks the registry name `telegram-downloads`, so it follows the record home rather than a literal. |

---

## info

Get details about a user, group, or channel.

```bash
.venv/bin/python ".claude/skills/telegram/scripts/telegram_client.py" info <chat>
```

Shows: ID, type, name, username, phone (if visible), member count, group/channel flags.

---

## reply

Reply to a specific message.

```bash
.venv/bin/python ".claude/skills/telegram/scripts/telegram_client.py" reply <chat> <msg_id> "reply text"
```

| Arg | Required | Description |
|-----|----------|-------------|
| `chat` | Yes | Chat identifier |
| `msg_id` | Yes | Message ID to reply to |
| `message` | Yes | Reply text |

---

## forward

Forward a message from one chat to another.

```bash
.venv/bin/python ".claude/skills/telegram/scripts/telegram_client.py" forward <from_chat> <msg_id> <to_chat>
```

| Arg | Required | Description |
|-----|----------|-------------|
| `from_chat` | Yes | Source chat |
| `msg_id` | Yes | Message ID to forward |
| `to_chat` | Yes | Destination chat |

---

## contacts

List phone contacts synced with Telegram.

```bash
.venv/bin/python ".claude/skills/telegram/scripts/telegram_client.py" contacts [--limit N]
```

| Arg | Required | Default | Description |
|-----|----------|---------|-------------|
| `--limit` / `-n` | No | 100 | Max contacts to show |

---

## mark-read

Mark all messages in a chat as read.

```bash
.venv/bin/python ".claude/skills/telegram/scripts/telegram_client.py" mark-read <chat>
```

---

## delete

Delete a message.

```bash
.venv/bin/python ".claude/skills/telegram/scripts/telegram_client.py" delete <chat> <msg_id>
```

| Arg | Required | Description |
|-----|----------|-------------|
| `chat` | Yes | Chat identifier |
| `msg_id` | Yes | Message ID to delete |

---

## Chat Identifier Resolution

The script resolves chat identifiers in this order:

1. **`me` / `self` / `saved`** — Your Saved Messages
2. **Numeric ID** — Direct chat/group/channel ID (e.g., `-100123456`)
3. **@username** — Telegram username
4. **+phone** — Phone number (must be in contacts)
5. **Username without @** — Tries as username
6. **Fuzzy name match** — Searches dialog list for closest match (>30% similarity threshold)

When fuzzy matching is ambiguous, use @username or chat ID for precision.
