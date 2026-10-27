# What's new in HEADING OS Official

This page lists what the Official distribution gained since its first release, newest first. Each entry says what changed for you and, where it matters, what you do about it.

The current version is HEADING OS 3.1. From 3.1 on, one number names a release: the tag on the `HEADING-OS-Official/heading-os` repository (`v3.1.0`) and the version the README shows under "Status" are the same, and they follow Semantic Versioning. The first three releases were tagged by date, `v2026.10.1` to `v2026.10.3`; those tags stay as they are.

To move an existing installation to a newer release, follow "Update" in `INSTALL.md`.

## Not yet released

These are on `main` and will be in the next tagged release. Until then `install.sh` installs `v3.1.0`.

### A cold read by a reviewer who never saw the work

`/goldfish` hands one document or change to a reviewer that starts without your conversation, your instruction files or memory, and reads only the document and a written standard. Use it before you approve a plan, send a brief, submit a proposal or tender answer, or trust that a document still describes what the code does. You answer each finding as FIX, CONTEST or ESCALATE. The full description is in `SKILLS-SYSTEM.md`. The tree now ships 70 skills.

## 3.1.0 (2026-10-27)

Tagged `v3.1.0` on 2026-10-27; `install.sh` installs it.

### New guides for everyday use

Five new pages join the install guide and the six tool guides in `docs/`:

- `WORKING-WITH-HEADING-OS.md`: the first day, an ordinary day, how a request becomes work, drafts and sending, handoffs, backups.
- `SKILLS.md`: the index of every skill that ships, with one page per group (`SKILLS-INTEL.md`, `SKILLS-COMMUNICATION.md`, `SKILLS-CONTENT.md`, `SKILLS-STRATEGY.md`, `SKILLS-DAILY.md`, `SKILLS-SYSTEM.md`). Each skill has what it does, how to call it, an example, what it needs, and when to use another one instead.
- `RULES-AND-PROTECTIONS.md`: the always-on rules and the checks that refuse an action, what you see when one refuses, and what to do.
- `WHATS-NEW.md`: this page.

### Seven more skills ship

The Official tree now ships 69 skills, up from 62. These seven were left out of the first three releases because their text was written around one company. Each now reads your company from your own records instead:

| Skill | What it gives you |
|---|---|
| `/ceo-intel` | A dated global and geopolitical brief, framed for your company. |
| `/competitor-intel` | Your company against one named competitor: gaps, overlap, strengths, a win strategy. |
| `/market-brief` | A sector or region brief: size, players, regulation, timing, your position. |
| `/intel-briefing-newsletter` | An intelligence newsletter for outside readers, rendered to HTML and PDF under your brand. It never sends itself. |
| `/investor-pitch` | Pitch deck content, slide by slide, with speaker notes. |
| `/investor-update` | A progress update for your current investors. |
| `/keynote-deck` | A deck for a conference, an important meeting or a government engagement. |

What you do: each of these starts by reading your business records (`business-info`, `strategy`, `current-data`, `pipeline`, `voice-guide`). If one is missing, the skill names it and tells you how to fill it, then continues with a neutral frame and invents nothing about your company, your numbers or your market. `/setup-wizard` writes most of them. Run it first. `SKILLS-INTEL.md`, `SKILLS-STRATEGY.md` and `SKILLS-CONTENT.md` describe each skill in full.

The newsletter's look comes from a settings file, `newsletter-brand.json`, beside `newsletter.css` in your brand kit: name, colours, fonts, logo, regions and section titles. Without either file it renders in a plain neutral style and says so on screen, so you always know which look you got. Nothing breaks.

### Nothing in the tree belongs to anyone

A full review of the published tree found business framing and machine names from the maintainer's own installation inside shipped skills, rules, scripts and comments. They are gone. The build that makes each release now refuses any such term, in a file's text or in its path, so a later change cannot quietly bring one back into a release. Skills that used to carry one company's framing now read yours from your records or write neutrally.

What you do: nothing. If a skill seems to know nothing about your business, fill in your records with `/setup-wizard`.

### The repository's own checks pass

From v2026.10.1 to v2026.10.3, the CI checks of the published repository failed on every run. The cause was simple. The engine's checks were looking for files the Official tree does not ship, and missed the files it adds, such as `install.sh` and the guides. The build now adapts those checks to what it actually ships, so they can pass. No check was weakened: an unlisted page or a dangling path still refuses, exactly as it did before, and the build itself verifies that in both directions.

Smaller changes in the same release:

- The README gained a "Status" section with the version, 3.1.0, and names the seven enforcement layers that `SECURITY.md` describes.
- A CI step that checked hashes of vendored skills is gone, because no vendored skill ships.
- The pinned versions of the GitHub Actions used by CI were brought up to date.
- `install.sh` now picks the newest versioned tag (`v3.1.0` and later) and falls back to the dated tags only while none exists. Before, it ranked `v2026.10.3` above `v3.1.0`, because 2026 is a larger number than 3.
- The README has a section on the VIP licence and links the guides online at headingos.com/docs. It no longer calls HEADING OS "source-available"; `LICENSE` and `NOTICE` are unchanged.
- Security reports go to `admin@headingos.com` with the subject line `HEADING OS security`, as the site says.
- New at the top of the repository: `CHANGELOG.md`, `SUPPORT.md` and `CONTRIBUTING.md`. Bug reports are welcome as issues; pull requests are not accepted.

## v2026.10.3 (2026-10-26)

### The six tool guides rewritten

`HINDSIGHT.md`, `LIFIC.md`, `HERDR.md`, `CLIPROXY.md`, `AGENTVIEW.md` and `YARDS.md` were rewritten from short notes into full guides for a new user on Linux, WSL or macOS: what the tool is, how to install and connect it, daily use with examples, troubleshooting, updating and removing it. Together they grew by about 2,000 lines. Start with `YARDS.md`.

Two small engine fixes came with them. One is a wording fix in the Lific rule, and the other is a fix in the yard preparation script that Herdr runs each time it creates a yard.

## v2026.10.2 (2026-10-26)

### A README for executives

The README was rewritten for an executive with no technical background: what HEADING OS does in one sentence, eight before-and-after scenarios with the command for each, a day with HEADING OS, security in plain words, install in three steps, the optional tools and an FAQ. Three illustrations sit in `docs/assets/`. No jargon comes before the install section.

### Install fixes

- `INSTALL.md` now gives the macOS commands for `git` and `gh` (`brew install git gh`, after Homebrew).
- `INSTALL.md` now explains how `/backup` and `scripts/push-all.py` behave with a data repository created with `--local-data`, which has no remote, and how to give it one: `gh repo create <name> --private --source <parent>/.heading-os-data --remote origin --push`.
- `install.sh` now picks the newest release only among tags of the form `vYEAR.MONTH.N`, ignoring any other tag, and sorts them in a way that also works with the `sort` that ships on macOS.

### License and notice

The contact address in `LICENSE` and `NOTICE` moved to the `headingos.com` domain. `NOTICE` now says plainly that the `*.example.*` files are empty templates, with field names and no values, and that every name of a person or an organisation in the documentation and the test fixtures is invented. Write in if one is not.

## v2026.10.1 (2026-10-26)

The first public release of the Official distribution:

- the engine without its private history, as one snapshot commit;
- 62 skills;
- `install.sh` and `docs/INSTALL.md`;
- the first versions of the six tool guides: Hindsight, Lific, Herdr, CLIProxyAPI, AgentView, and the HELM and yards method;
- the empty data template, `heading-os-data-template`, from which `install.sh` builds your private data repository;
- CI workflows for tests, a dependency audit and an OpenSSF Scorecard.
