<!--
Pull requests are accepted BY INVITATION. If you have not already discussed this
change in an issue and been invited to send a PR, please open an issue first.
Unsolicited PRs may be closed with a pointer to that process — it is how the
project stays coherent, not a judgement of your work.
-->

## Linked issue

Closes #<!-- issue number you were invited from -->

## What this changes

<!-- A short, plain description of the change and why. -->

## Checklist

- [ ] This change was discussed in an issue and invited
- [ ] `python scripts/run-tests.py` passes
- [ ] I added a test only if this change guards against private data reaching this public repository, against the engine and the data repository merging, or against a worktree acting on something live (a running process or an outside party); otherwise I wrote no test, I ran the change to show it works, and the commit message carries a line `Fix-Note: <what broke> | <what fixed it> | <why no test>`
- [ ] `pre-commit` passes (ruff + secret scan); I did not use `--no-verify`
- [ ] No secrets or personal data in any tracked file
- [ ] Scope is limited to the linked issue; no unrelated refactoring
- [ ] No new dependencies (or they were agreed in the issue and pinned to exact versions)
