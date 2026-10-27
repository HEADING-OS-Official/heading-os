# Scheduled and background tasks

How a scheduled task behaves in this workspace, and which mechanism survives a
reboot.

Consumed by: `.claude/rules/development-standards.md` (pointer), and anyone
wiring a recurring job.

Last Updated: 2026-09-25

Scheduled tasks created via the `CronCreate` tool are recorded in
`.claude/scheduled_tasks.json`, but they are **session-scoped**: they fire only
in the session that created them, or when it is resumed via `claude --resume` /
`--continue` before expiry (recurring tasks expire 7 days after creation). A
fresh session does NOT re-activate them. For reminders that must fire regardless
of session lifecycle (and catch up after the machine was off), use the durable
reminders system: `scripts/reminders.py` (CLI) + the `reminders.timer`
systemd-user timer -> `scripts/reminders-notify.py` -> Telegram, with a `/prime`
backstop. See `specs/2026-07-14-durable-reminders-design.md` under registry
name `superpowers-specs`.

To view active scheduled tasks: `cat .claude/scheduled_tasks.json | python -m json.tool` (or just read the file directly).

To cancel a task: use `CronDelete` with the task ID shown in the JSON. Editing the file by hand is not supported - the runtime will overwrite changes.

If the file grows large or contains orphaned tasks (e.g., after long periods between sessions), list them via `CronList` and prune with `CronDelete`. There is no automatic cleanup.

Scheduled tasks are machine-local - they do NOT sync between machines. Each machine maintains its own scheduled set.
