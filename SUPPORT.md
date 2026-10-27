# Support

HEADING OS is free to download, install and use. This page says where to look when something is unclear or broken, and where to write.

## Read first

- **Installing:** [docs/INSTALL.md](docs/INSTALL.md) covers the prerequisites, what `install.sh` does, how to verify the result, how to update, and how to fix common problems.
- **Daily use:** [docs/WORKING-WITH-HEADING-OS.md](docs/WORKING-WITH-HEADING-OS.md), [docs/SKILLS.md](docs/SKILLS.md) and [docs/RULES-AND-PROTECTIONS.md](docs/RULES-AND-PROTECTIONS.md).
- **Optional tools:** each has its own guide in [docs/](docs/): Hindsight, Lific, Herdr, CLIProxyAPI, AgentView, and the HELM and yards method.
- **Online:** the same 17 guides are at [headingos.com/docs](https://headingos.com/docs/).
- **What changed:** [CHANGELOG.md](CHANGELOG.md) and [docs/WHATS-NEW.md](docs/WHATS-NEW.md).

Before you write, run the two checks the install guide names. Their output is the first thing anyone helping you will ask for:

```bash
bash scripts/setup-platform.sh --check
uv run python scripts/workspace-health.py
```

## Where to write

- **A bug:** open an issue with the bug report form. [CONTRIBUTING.md](CONTRIBUTING.md) says what makes a report easy to act on.
- **A security problem:** never in a public issue. Email `admin@headingos.com` with the subject line `HEADING OS security`, as [SECURITY.md](SECURITY.md) describes.
- **Anything else:** `admin@headingos.com`.

Support here is best effort, from the author's own time. There is no promised response time for issues or mail.

## Installation and support you can rely on

The [VIP licence](https://headingos.com/vip.html) is the paid route: a team installs HEADING OS on your own infrastructure, sets it up around your mail, calendar, voice and records, and supports it for twelve months. The first year is from $24,850, and it starts with a call; [request one](https://headingos.com/vip.html#request) and the reply comes within 48 hours.

Claude Code, Hindsight, Lific, Herdr, CLIProxyAPI and the other tools HEADING OS works with are other people's projects. Questions about them go to their own authors.
