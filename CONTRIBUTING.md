# Contributing

Thank you for wanting to make HEADING OS better. Two kinds of help are welcome here, and one is not accepted.

## Welcome

- **Bug reports.** Something behaves differently from what the guides say: open an issue with the bug report form.
- **Ideas.** A feature you would use: open an issue with the idea form, and describe the problem before the solution.
- **Security reports.** Never as an issue. Email `admin@headingos.com` with the subject line `HEADING OS security`; [SECURITY.md](SECURITY.md) has the details.

## Not accepted: pull requests

Pull requests from outside are closed without review. The reason is the licence, not the quality of the work. HEADING OS is licensed under PolyForm Internal Use 1.0.0 or PolyForm Noncommercial 1.0.0, and the commercial rights (selling it, and paid installation, configuration or support for others) are reserved to Misha Hanin and the entities he approves in writing ([LICENSE](LICENSE), [NOTICE](NOTICE)). That reservation stays simple only while no outside code is taken in. If you have found a fix, describe it in the issue: the file, the line and what you changed is enough for it to be made here.

## A report that gets fixed

The bug report form asks for the first four; add the last two in its output field. The more exact they are, the faster the fix.

1. **What happened, and what you expected.** One sentence each.
2. **The exact steps,** in order, from a fresh session where possible.
3. **The output,** pasted as text rather than a screenshot.
4. **Your system:** the operating system (macOS, Linux, or Windows with WSL2) and the output of `python --version`.
5. **The release you run:** `git describe --tags` in the engine folder.
6. **The checks:** the output of `bash scripts/setup-platform.sh --check`. If a safety check refused an action, paste its message whole: the refusal names the rule, and [docs/RULES-AND-PROTECTIONS.md](docs/RULES-AND-PROTECTIONS.md) explains each one.

Search the existing issues first; a duplicate splits the conversation.

**Remove secrets and personal data before you paste.** API keys, tokens, passwords, mail addresses, names from your records and anything from your private data repository stay out of an issue. An issue is public once the repository is, and it cannot be fully unwritten.

