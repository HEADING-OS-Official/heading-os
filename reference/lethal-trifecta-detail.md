# Lethal trifecta: the self-notification exception written out

The self-notification boundary's limit, which the always-on rule bounds and defers here.

Consumed by: .claude/rules/lethal-trifecta.md

Last Updated: 2026-10-29

## What the self-notification boundary does NOT close

Read this before changing how `scripts/utils/telegram_notify.py` resolves its allowlist.

The read is `os.environ`, not the `.env` FILE, so a value the running process assigned to one of those names is indistinguishable from one the operator typed. Reading the file instead is a WORSE trade, not a better one: `tests/conftest.py` contains the whole suite by blanking those names in `os.environ`, so a file-reading resolver would let a test run message the operator, and a systemd unit passing the target via `Environment=` would go dark. An adversary who can assign to `os.environ` in this process can also call `TelegramBot` directly and skip this module, so the boundary buys nothing against that one. Do not "harden" it to a file read without settling both of those first. The reasoning lives in full at `own_targets()`.
