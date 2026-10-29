# Console-First: No Web-Dashboard Dependency

Last Updated: 2026-10-29
Last Verified: 2026-09-21

Always-active rule. Every capability built in or for this workspace must be fully operable from the terminal, a CLI and Claude Code chat; a web surface is a convenience layer, never a dependency. CEO directive, 2026-06-03. Depending on a running headless process is allowed; depending on a rendered web page is a defect, and a web-only capability is a finding to fix before done, not a note to defer.

**There is no web surface in this workspace today** (HOS-7, 2026-09-21), so this rule fires when somebody proposes a browser page.

Ship the non-web path first, then confirm it in the completion line, e.g. `Console-first: CLI + chat paths verified; browser optional.` The four build requirements and three validation questions: `.claude/rules/console-first-build.md`, path-scoped to `scripts/**`, `.claude/skills/**` and `.claude/agents/**`, so it loads on the first write to a capability file. Rationale and scope: `ARCHITECTURE.md` § 5 under registry name `spec-docs`.
