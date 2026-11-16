# Changelog

All notable changes to HEADING OS Official are recorded in this file, newest first.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). From 3.1.0 on, releases follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html), and the tag, the version in `pyproject.toml` and the one the README shows under "Status" are the same number. The first three releases were tagged by date, `v2026.10.1` to `v2026.10.3`, and those tags stay as they are. The same history, written for users with what to do about each change, is [docs/WHATS-NEW.md](docs/WHATS-NEW.md).

## [3.2.0] - 2026-11-16

New tools for design files and for watching a push, a new status line and context bar, Hindsight parts that update themselves, and every dependency brought up to date, majors included. Python 3.11.5 is now the minimum. The full explanation, with what to do about each change, is in [docs/WHATS-NEW.md](docs/WHATS-NEW.md).

### Added

- `scripts/fig-design.py`: reads a Figma `.fig` design-system file and renders it. `info` (counts and fonts), `render` (frames to SVG and PNG), `shells` (empty app shells), `kit` (`tokens.css` and `component-specs.txt`) and `shot` (SVG or HTML to PNG). The file is treated as untrusted: nothing is extracted to disk, every size is capped, and a bad file ends with one `fig-design:` line and exit 2.
- Push progress: `scripts/push-all.py` records its steps as it runs, the status line draws a bar with the step, the time spent and the time left, and `scripts/push-progress.py` prints the same (`--json` for the record). The run sends one message at the start and one at the end to your own Telegram sink; `--no-notify` turns both off.
- A two-row status line: a HELM or YARD badge, the unattended state with its two counters, the Lific issue in work, the yards and how many need you, each repository's state and the model. Nothing on it is cut; a long row wraps. A refresher reads herdr, Lific and git at most every 30 seconds, so drawing it never waits on them.
- `scripts/check-frontmatter.py`: lists the tracked Markdown files in your data repository, and every `SKILL.md`, whose YAML header is broken. Read-only; `--json` for one JSON object.
- The Hindsight CLI and the Hindsight coding-agents plugin update themselves (`tier: auto` in `config/update-registry.yaml`): each verifies what it downloads, keeps a backup, checks the result and puts the backup back on a failure. The server and web console updaters ship as well, but stay `observed` here, because they reach a separate Hindsight server through the `HINDSIGHT_ADMIN_SSH_HOST` ssh alias in `.env`.

### Changed

- The context bar measures against the whole context window and marks the attention point, the `/second-wind` point, auto-compaction and the end of the window; it forecasts how many turns are left before `/second-wind`. Its labels sit on rows of their own, never on the bar.
- Every engine dependency is on its current release. Majors taken: anthropic 2.8, openai 3.49, langfuse 5.2, ruff 0.17, playwright 1.65, fastapi 0.162, pymdown-extensions 13.1. With anthropic 2, no request to a Claude model carries `temperature`.
- `requires-python` is `>=3.11.5` (it was `>=3.11`), because pymdown-extensions 13 needs it.
- `scripts/check-unit-drift.py` reports a unit the service layer adopts as `[adopted]`, not as drift with advice to re-run the installer.
- The settings templates set `statusLine.refreshInterval` to 2 seconds, so the status line keeps moving while a session is idle.

### Fixed

- Exchange calendar sync no longer fails when a meeting has an attendee with no email address; it uses the attendee's name, or leaves the attendee out.
- Langfuse traces carry the trace id and the vendor, model, skill and fallback tags again; langfuse 5 had removed the call that set them, and the failure was logged only at debug level.
- The document walkers behind the memory index skip the runtime store at every level, so the hourly index refresh no longer reads test fixtures there and fails.
- A session working in a subdirectory of the engine keeps its checkpoint state where the engine root keeps it, never inside the engine tree.
- The frontmatter reader warns, naming the file, when it falls back to a partial reading of a broken header, and a bad date or an unknown tag in a header no longer stops a reader with a traceback.
- The status line fits its row in a yard as in HELM, counts wide characters as two cells, never runs git in the data repository from a yard, and its refresher runs on Windows.
- The context bar never draws wider than its row on a narrow terminal.
- A push whose process died during a long step keeps its "stopped" result on the status line for the full ten minutes.

## [3.1.4] - 2026-11-04

### Added

- The installer asks for consent before its first change, can be paused and stopped, refuses clearly, and reports what a removal took away.
- macOS: Hindsight runs under launchd, the time zone is set before the first timer, doctor reads launchd, and a free-space check runs first.

### Fixed

- `scripts/utils/env_exec.py` refuses a FIFO in place of the env file instead of hanging on it.
- A system Node.js passes the Node step, and a daemon that has just started is not reported down.
- pyasn1 0.6.5.

## [3.1.3] - 2026-11-03

### Added

- One resumable installer, `scripts/heading-install.py`, with `install.sh` as its bootstrap, a progress window, and the setup wizard inside Herdr. It installs and wires Lific, Herdr, CLIProxyAPI, Hindsight, the local search index (ollama with bge-m3 and its hourly refresh), Exchange mail and calendar, and Google mail and calendar through your own OAuth app.
- One service layer installs every engine daemon and timer, on systemd and on launchd.
- `doctor` checks every component end to end, and the `heading` command opens Claude in the engine.
- An install runbook that Claude follows, and the HEADING OS Admin Console: one static page linking every web interface and the guides.
- Secrets go into `.env` from a hidden terminal prompt, never through the chat; one sign-in key can be shown in a window of its own.
- The context-bar mod: a band above the prompt showing what fills the context window, on by default.

### Fixed

- The yard write guard refuses writes into the main clone's git directory however they are spelled.
- A fresh data repository backs up and no longer blocks the engine push.
- Exchange reads unknown Windows time zones correctly.

## [3.1.2] - 2026-11-01

### Added

- Your own Telegram sink can receive any file, through the same allowlist as a notification.
- `scripts/review-branch.py` runs a voting panel of reviewers, keeps findings that were already there from blocking, and stops after three rounds.

### Changed

- The release gate's outbound wall also covers MCP sends, shares and deletes, Exchange invites and mail deletion, and git subcommands that commit.

### Fixed

- The Hindsight status report leaves out failures a later success has superseded, and deferred refreshes are no longer reported as a stall.
- The nightly refresh unit quotes its PATH, so systemd keeps every directory.
- Background herdr waiters exit when their pane is gone.
- An expired CLIProxyAPI login stops an update cleanly.

## [3.1.1] - 2026-10-30

Mostly a tidy-up of how existing things work, plus three new tools. Every command from 3.1.0 still works. The full explanation, with what to do about each change, is in [docs/WHATS-NEW.md](docs/WHATS-NEW.md).

### Added

- `/teach`: a course over several sessions (goal, sources, short interactive lessons, reference sheets, glossary, learning notes), with every course file in your data repository. Typed only. Adapted from Matt Pocock's `teach` skill ([mattpocock/skills](https://github.com/mattpocock/skills), MIT License); the licence is in `.claude/skills/teach/LICENSE-MIT.txt` and `NOTICE` credits it.
- `scripts/rules-ab.py`: tests a change to your instructions in real, sealed sessions, old against new, scored by a blind judge; also shows which rules loaded on which tool call.
- `scripts/context-floor-audit.py sessions`: what fills the context window across your recent sessions, as totals only.

### Changed

- The always-on rules say the same thing in less text: procedures, history and duplicates moved out to the files the rules point at.
- `/pitch` (investor, update, keynote), `/image` (prompt, generate) and `/linkedin` (post, series, archive) replace eight similar skills. The old names (`/investor-pitch`, `/investor-update`, `/keynote-deck`, `/image-prompt`, `/flux-image`, `/linkedin-post`, `/linkedin-series`, `/linkedin-archive`) remain as typed shortcuts.
- Eight tools run only on their typed name: `/context7`, `/docparse`, `/google-contacts`, `/memory-hygiene`, `/pencil-export`, `/playwright`, `/radar`, `/setup-browser-cookies`.
- The skill router is a short policy plus a collision map; the full table moved to `reference/skill-router/<category>.md`. `scripts/skill-trigger-test.py --competitive` tests routing the way a live session chooses.
- Hindsight recall answers come back compact by default; `verbose` returns the full record.
- The rule-inventory and secret-baseline checks run in `scripts/push-all.py` before a push, not only in CI afterwards.
- `scripts/review-branch.py` reviews code first and splits a large change into parts instead of cutting it.
- `scripts/herdr-brief.py` sends a yard brief only when a `/goldfish ready` review covers its exact text.
- A commit message carrying a Claude co-author line is refused.

### Fixed

- `install.sh` acts only on the data repository it creates. A `HEADING_OS_DATA` inherited from another installation on the same machine used to send the hook step to that installation's data repository; the installer now points the variable at its own data repository before any step, warns when an inherited value differs, and checks discovery with the variable unset, as a new shell will find it.
- `/dream` records its consolidation step again: `scripts/retire-memory.py` writes the trace row itself, which the overlay write guard refused from an inline script.
- `scripts/hindsight-sync.py blocked list` shows a version-cleanup batch the safety limit blocked, and a confirmed command runs it; before, nothing could release it.
- Deleting a Telegram message is its own action in the approval gate, released by a delete instruction rather than a send instruction.
- A request to compare with an unnamed competitor routes to `/competitor-intel`, which asks which competitor first.
- The status line in a yard names the yard once instead of "branch on branch"; the automatic continuation message is a few lines long.

## [3.1.0] - 2026-10-27

The tag was re-cut on 2026-10-28 on a single commit, and the release includes `/goldfish`.

### Added

- `/goldfish`: a cold read of one document or change by a reviewer that starts without your conversation, your instruction files or memory, against a written standard per mode (plan, reader, ready, proposal, drift, code). Its standards are adapted from Google's [Elephant-Goldfish Model](https://github.com/google/elephant-goldfish-model), licensed Apache-2.0.
- Guides for everyday use in `docs/`: `WORKING-WITH-HEADING-OS.md`, `SKILLS.md` with one page per skill group (`SKILLS-INTEL.md`, `SKILLS-COMMUNICATION.md`, `SKILLS-CONTENT.md`, `SKILLS-STRATEGY.md`, `SKILLS-DAILY.md`, `SKILLS-SYSTEM.md`), `RULES-AND-PROTECTIONS.md` and `WHATS-NEW.md`. The same guides are online at [headingos.com/docs](https://headingos.com/docs/).
- Seven skills that read your company from your own records: `/ceo-intel`, `/competitor-intel`, `/market-brief`, `/intel-briefing-newsletter`, `/investor-pitch`, `/investor-update` and `/keynote-deck`. With `/goldfish`, the tree ships 70 skills, up from 62.
- `CHANGELOG.md`, `SUPPORT.md` and `CONTRIBUTING.md`.
- A README section on the [VIP licence](https://headingos.com/vip.html), and a "Status" section with the version.
- The README opens with what changes on day one, and its VIP section says what the licence adds to the public version, as [headingos.com](https://headingos.com) does.
- The README names HEADING OS the personal project of [Misha Hanin](https://www.linkedin.com/in/mishahanin/) and [Jochanan Sommerfeld](https://www.linkedin.com/in/jochanansommerfeld/), co-architect of the whole HEADING OS system, as [headingos.com](https://headingos.com/about.html) does; `NOTICE` carries the same credit.

### Changed

- Versions follow Semantic Versioning, starting at 3.1.0.
- The README no longer calls HEADING OS "source-available"; it says what the licence allows, as [headingos.com/license.html](https://headingos.com/license.html) does. `LICENSE` is unchanged, and `NOTICE` changes only by the co-architect credit.
- Security reports go to `admin@headingos.com` with the subject line `HEADING OS security`, as on [headingos.com/security.html](https://headingos.com/security.html). GitHub private vulnerability reporting works too once the repository is public.
- The issue form's contact links point at the guides and the security page on headingos.com.
- The pinned versions of the GitHub Actions used by CI were brought up to date.
- The authors' credit in the README, `NOTICE` and this file links [Misha Hanin](https://www.linkedin.com/in/mishahanin/)'s profile as well as the co-architect's.
- A continuation after `/clear` follows the handoff's "Safe resume" block.

### Fixed

- The repository's own CI checks failed on every run from v2026.10.1 to v2026.10.3, because they looked for files this tree does not ship and missed the files it adds. The build now adapts them to what it ships; no check was weakened.
- `install.sh` ranked the dated tags above `v3.1.0`, because 2026 is a larger number than 3, so it would have kept installing `v2026.10.3`. It now picks the newest versioned tag, and falls back to the dated ones only while none exists.

### Removed

- Business framing and machine names from the maintainer's own installation, found in shipped skills, rules, scripts and comments. The release build now refuses any such term, in a file's text or its path.
- The CI step that checked hashes of vendored skills, since no vendored skill ships.

## [v2026.10.3] - 2026-10-26

### Changed

- The six tool guides (`HINDSIGHT.md`, `LIFIC.md`, `HERDR.md`, `CLIPROXY.md`, `AGENTVIEW.md`, `YARDS.md`) rewritten from short notes into full guides for a new user on Linux, WSL or macOS.

### Fixed

- A wording fix in the Lific rule.
- A fix in the yard preparation script that Herdr runs each time it creates a yard.

## [v2026.10.2] - 2026-10-26

### Changed

- The README rewritten for an executive with no technical background, with three illustrations in `docs/assets/`.
- The contact address in `LICENSE` and `NOTICE` moved to the `headingos.com` domain. `NOTICE` now says that the `*.example.*` files are empty templates and that every name of a person or an organisation in the documentation and the test fixtures is invented.

### Fixed

- `docs/INSTALL.md` gives the macOS commands for `git` and `gh`.
- `docs/INSTALL.md` explains how `/backup` and `scripts/push-all.py` behave with a data repository created with `--local-data`, and how to give it a remote.
- `install.sh` picks the newest release only among tags of the form `vYEAR.MONTH.N`, and sorts them in a way that also works with the `sort` that ships on macOS.

## [v2026.10.1] - 2026-10-26

### Added

- The first release of the Official distribution: the engine without its private history, as one snapshot commit, with 62 skills.
- `install.sh` and `docs/INSTALL.md`.
- The first versions of the six tool guides: Hindsight, Lific, Herdr, CLIProxyAPI, AgentView, and the HELM and yards method.
- The empty data template, `heading-os-data-template`, from which `install.sh` builds your private data repository.
- CI workflows for tests, a dependency audit and an OpenSSF Scorecard.

[3.1.0]: https://github.com/HEADING-OS-Official/heading-os/compare/v2026.10.3...main
[v2026.10.3]: https://github.com/HEADING-OS-Official/heading-os/tree/v2026.10.3
[v2026.10.2]: https://github.com/HEADING-OS-Official/heading-os/tree/v2026.10.2
[v2026.10.1]: https://github.com/HEADING-OS-Official/heading-os/tree/v2026.10.1
