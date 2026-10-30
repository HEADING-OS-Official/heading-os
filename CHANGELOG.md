# Changelog

All notable changes to HEADING OS Official are recorded in this file, newest first.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). From 3.1.0 on, releases follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html), and the tag, the version in `pyproject.toml` and the one the README shows under "Status" are the same number. The first three releases were tagged by date, `v2026.10.1` to `v2026.10.3`, and those tags stay as they are. The same history, written for users with what to do about each change, is [docs/WHATS-NEW.md](docs/WHATS-NEW.md).

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
