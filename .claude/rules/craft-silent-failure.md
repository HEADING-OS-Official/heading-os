---
paths:
  - "tests/**"
  - "scripts/**"
  - ".claude/hooks/**"
  - ".claude/skills/**"
  - ".claude/agents/**"
  - ".claude/rules/**"
  - "reference/**"
  - "config/**"
  - "conftest.py"
---

# Craft Whose Failure Is Silent

Last Updated: 2026-09-15
Last Verified: 2026-09-15

Path-scoped rule. Eighteen rules about tests, guards, indexes and scheduled
jobs, every one of them written down because the failure it describes produced a
GREEN result rather than a red one: a guard that refuses nothing, a loop that
asserts nothing over an empty corpus, a timer that declares what it never runs.
Nothing announces these. The tree looks fine and the claim is false.

They left the operator's memory index on 2026-09-15, when it neared the
harness budget that truncates it from the end. The `paths:` list above is
deliberately wide, operator instruction that day: **"make it wide. we cannot
afford rules that do not work."** (translated)

The first seventeen lines below are reproduced VERBATIM from the index, dashes and all, and the
`voice.md` carve-out for reproduced text governs them. The last one was added on
2026-09-27 from a proposal the operator accepted that day, and says where its
evidence stands. Never shorten a label to
save room; a rule the reader cannot act on from the page has already failed.

## The rules

- Every guard test needs a case that makes the guard actually REFUSE, plus the honest case that must still pass. A guard that refuses everything satisfies every refusal test.
- When a fix turns a NEIGHBOURING test red, read that test's assertion before doubting the fix. A test written while the defect was live can assert the defect's own output; one asserted a file's move to its own path as a requirement for a day.
- Scan any script past a few hundred lines for names assigned and never read. Each is usually a contract written down and never enforced: a journal's column header sat unused while its sibling was checked every run.
- A test whose assertions all sit inside a loop over a discovered corpus passes over an empty corpus. Assert the size outside the loop, with the measured number and its date.
- Never verify a security control by grepping source text, and ask the AST rather than the text: a substring scan goes red the moment a fix quotes the bad pattern to explain it.
- Drive the real entry point and assert the observable consequence — an exit code, bytes not written, a subprocess not spawned. Never restate the guard's own condition inside the test.
- Mutation-verify every fix by planting breaks in the SOURCE, never in an assertion, and state the fraction caught. A survivor is a gap, dead code or an equivalent mutant; it is never made to pass by weakening the test.
- Never run a mutation harness or a probe against the real `HEADING_OS_DATA`, and never pass a relative `--basetemp`: both reach the operator's live files.
- Have a mutation harness write its backup to a FILE before the first edit, and make it resumable by mutant index. A kill skips `finally`, and on an untracked file every git-shaped check answers "no change" over bytes that are currently broken.
- Before running anything that prunes or empties a store, enumerate what it holds and ask per kind whether it can be re-derived from disk. "It rebuilds from source" is a claim about one source, and a store acquires others without anyone deciding it has.
- A test that reads the host clock is not a test. A different test failing on each full run means machine load; the SAME one failing intermittently means a cyclic fixture.
- Local green is not CI green: the runner has no private overlay, so every overlay-resolving guard answers differently there.
- Re-measure every skipped test's reason. A skip reports silence, and most named reasons decay into defects.
- Construct every APScheduler with `job_defaults=JOB_DEFAULTS`, never per job. The 1-second misfire default silently discarded 1059 of 1440 runs.
- Every scheduled task ships as a systemd-user timer with all three of `Persistent=true`, `systemctl --user enable` and `loginctl enable-linger`. A unit tells you what it declares; the installer tells you whether it runs.
- Wait for a timer's pass in flight to finish before `systemctl --user daemon-reload` or reinstalling its units. A reload during a run makes systemd drop the rest of the unit's command list while the run still exits successfully. Evidence: stated as measured on 2026-09-22 in the message of commit `c6f2fc5c`; the primary measurement record was not found on 2026-09-27.
- Check a systemd unit's inherited PATH against your login shell's. A gap makes tool-gated tests skip silently and mark the tree green.
- Store the model identifier AND the dimension count in every index you build. Two embedders that agree on a number do not agree on a meaning, and the search fails silently rather than refusing.

## Where the reasoning is

Each rule stands on an incident with a date and a measurement, and those records
are not here. Ask Hindsight, the workspace's memory for records since
2026-10-04, by the rule's own words: in a session through the `recall` tool of
the `hindsight-main` MCP server, from a terminal through

    hindsight memory recall main "<the rule, in your own words>"
