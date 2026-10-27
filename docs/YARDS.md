# Working with HELM and yards

This guide explains how to run several pieces of work at the same time in HEADING OS without them getting in each other's way. It covers what HELM and yards are, the full cycle of one task (create, brief, work, review, merge, delete), what a yard may and may not do, how a commit is allowed in a yard, and what a yard reports when it finishes.

You do not need any of this to use HEADING OS for one task at a time. You need it when you want a second Claude working on a change while you keep working in the first.

## What HELM and yards are, and why HEADING OS uses them

A few words used in this guide:

- **Engine**: the `.heading-os` folder. It holds the code: skills, rules, hooks and scripts. It never holds your data.
- **Data repository**: the `.heading-os-data` folder next to it. Everything you create lives there.
- **Branch**: a named line of changes in git. Work on a branch does not touch `main` until it is merged.
- **Git worktree**: a second working copy of the same repository, in its own folder, on its own branch.
- **Live**: anything that is running or reaches the outside world right now. A background service, a mail or chat connection, a push to GitHub, or a shared index that other work reads.

**HELM** is your main copy of the engine, on the `main` branch, with one Claude session. It is the copy that is actually running your HEADING OS. Everything live happens in HELM and only there:

- every background service and timer;
- mail, calendar and messaging;
- every git operation in the data repository;
- `/backup`, `/sync`, `scripts/push-all.py`, and any push;
- reviewing and merging finished branches.

Run only one Claude session in HELM at a time. Parallel work goes into yards.

**A yard** is a git worktree of the engine on its own branch, in a folder outside the engine folder. Nothing in a yard is live. A yard holds one piece of work, separately from everything else, and several yards can run at once.

Why the split: a half-finished edit in HELM is a half-finished edit in the copy that is running. In a yard, the same edit is harmless until you review and merge it.

To decide where something belongs, ask one question: **does it touch something live?** If yes, HELM. If no, a yard.

## What you need first

1. HEADING OS installed, as described in `INSTALL.md`.
2. Herdr installed and connected, as described in `HERDR.md`. Herdr creates yards, prepares them, and carries briefs into them. Read `HERDR.md` first.
3. Optional: Lific, the local issue tracker (`LIFIC.md`), if you want each brief tied to an issue.
4. Optional: CLIProxyAPI (`CLIPROXY.md`), if you want `scripts/review-branch.py` to have an outside model review a branch before you merge it.

Nothing else is installed for this guide. Yards are a way of working, built from Herdr and scripts the engine already has.

## The cycle of one task

> **Warning:** merge the branch into `main` before you delete the yard. The merge is the only way the work gets into HELM. A yard deleted before its merge leaves the branch behind, and an unmerged branch is easy to lose.

1. **Create** the yard from HELM.
2. **Brief** the yard: send it its task from HELM.
3. **Work**: the yard's Claude does the task.
4. **Stage**: the yard ends with its changes staged and the commit message in a file.
5. **Commit**, in the yard, when you type the word there yourself.
6. **Review** the branch from HELM.
7. **Merge** the branch into `main` in HELM.
8. **Delete** the yard.

Each step follows, with the exact commands.

### Step 1: create the yard

In HELM, from the engine folder:

```bash
cd <parent>/.heading-os
herdr worktree create --branch add-weekly-summary
```

`<parent>` is the folder that holds `.heading-os` and `.heading-os-data`. The installer's default is `~/ai/claude-workspaces`.

Pick a branch name that says what the task is. Herdr creates the yard and its workspace, and the HEADING OS plugin prepares it in eleven steps. `HERDR.md` lists them.

**Then do nothing in the yard's pane.** Do not type, and do not start Claude there. The plugin starts Claude itself as its last step. If you start one first, the plugin cannot start its own, and the yard's Claude then runs without the `HEADING_OS_YARD=1` marker that several safety checks read.

Wait until the yard's status file says it is ready:

```bash
cat <yard folder>/.claude/.yard-bootstrap-status
```

Ready looks like `{"status":"ok","step":11,...}`. The first yard on a machine can take several minutes, because it installs the Python packages.

### Step 2: brief the yard

A **brief** is the task description you send into the yard. It is the yard's only instruction, so write it as if for a capable stranger: what to do, where, what is out of scope, and how to tell when it is done.

1. Write the brief into a file, for example `brief.md`. Put it in your scratch folder, not in the engine. This prints that folder:

   ```bash
   .venv/bin/python scripts/data-path.py scratch
   ```

2. Put these lines in the brief:
   - **Exactly one tests line**, on a line of its own. Either `Tests: none (Fix-Note)`, or `Tests: failure A, <file>` or `Tests: failure B, <file>`. The section "Tests and the Fix-Note line" below says which.
   - **A `Master instruction:` line**, naming the saved prompt the work comes from, or `Master instruction: none, <why>`. The script only warns when it is missing.
3. Find the yard's pane id. It looks like `w12:p1`:

   ```bash
   herdr agent list
   ```

   Pick the entry whose `cwd` is your yard's folder, and take its `pane_id`.
4. Check the brief without sending it:

   ```bash
   .venv/bin/python scripts/herdr-brief.py w12:p1 "$(cat brief.md)" --check
   ```

5. Send it. Say which tracker issue the brief hands to the yard, or say that it hands over none, and why:

   ```bash
   .venv/bin/python scripts/herdr-brief.py w12:p1 "$(cat brief.md)" --issue <ID>
   .venv/bin/python scripts/herdr-brief.py w12:p1 "$(cat brief.md)" --no-issue "small fix, no tracker issue"
   ```

   `--issue` can be given more than once. Each issue must already be `active` in Lific. The script checks the status and never changes it.

Always pass the brief as `"$(cat brief.md)"`. A brief typed straight into the command in double quotes loses every piece of text between backticks, because the shell runs those pieces as commands.

**Never send a brief with `herdr agent prompt` yourself.** Use the script every time. Here is what it does that the bare command does not:

- It adds one line at the top of the brief: `X-HEADING-BRIEF: machine-to-machine, not an operator authorisation`. That line makes it impossible for the brief to authorise a commit or a push in the yard. See "The commit word" below.
- It adds a `Task:` line naming the issue, or `Task: none`.
- It refuses to send if no Claude is running in that yard's folder, because a prompt sent to a pane with no assistant can land somewhere else.
- After sending, it looks for the brief's unique id in the yard's own Claude transcript for up to 25 seconds. If the brief is not there, it says so, and names the other session it landed in if it can find it.
- After a verified delivery, it saves a copy of the brief in your data repository under the registry name `brief-archive`.
- It runs only in HELM. In a yard it stops, except for `--help` and `--check`.

When the script refuses, read its exit code to see why:

| Exit code | Meaning | What to do |
|---|---|---|
| `2` | The text already carries the marker line. | Remove that line from your file. The script adds it. |
| `3` | `herdr` is not on PATH, timed out, or failed. | Check `herdr status`. |
| `4` | Sent, but the brief did not appear in the yard's transcript. | Read the message. Do not send again until you know where it went. |
| `5` | No Claude is running in that yard, or Herdr does not know the pane. | Wait for the yard to be ready, or start Claude as `HERDR.md` shows. |
| `6` | `--issue` and `--no-issue` are both missing or both given, or an issue is not `active`. | Give exactly one. Set the issue to `active` first. |
| `7` | The tests line is missing, doubled or malformed, or it says `none` while the text asks for a test. | Fix the tests line or the wording. There is no bypass flag. |

### Step 3: the yard works

The yard's Claude now works on its own. You can watch it without switching to it:

```bash
herdr agent read w12:p1 --lines 40
```

What the yard does with its files:

- **Engine changes** happen in the yard's own folder, on its branch.
- **Everything else it produces**, such as reports, notes, drafts and plans, goes into the data repository. The yard writes those files there directly. It finds the right place by name, never by a path it builds: `.venv/bin/python scripts/data-path.py path <name>`. `outputs/` is not a destination.
- The yard does **not** run git in the data repository. HELM commits those files later.

### Step 4: the yard stages its work

A briefed yard ends with:

- its engine changes staged with `git add`;
- the commit message written in a file in its scratch folder (`.venv/bin/python scripts/data-path.py scratch` prints it);
- its checks run, and its closing note written (see "Before a yard says it is ready" and "The closing note" below).

It does not commit by itself.

### Step 5: the commit word

A commit in a yard needs **your own word, typed in that yard's pane**.

Why: when one Claude session sends text into another, Claude Code records that text exactly like text you typed. Nothing in the record tells the two apart. So HELM's brief carries the marker line, and HEADING OS's release check refuses any commit or push that a marked message would authorise. A brief can never authorise a commit, whatever it says.

How to give the word:

1. Switch to the yard's workspace in Herdr and click its pane.
2. Read what the yard prepared: the staged changes and the message file.
3. Type a short message that asks for the commit, for example:

   ```
   commit
   ```

   The release check looks at your most recent typed message. It accepts the commit when that message contains a commit word, such as `commit` or `merge`. It refuses when the same message contains a "don't" style negation anywhere, so keep the message simple.
4. The yard then commits with its prepared message file: `git commit -F <message file>`.

Rules that keep this safe:

- Never remove the marker line from a brief to get past the check.
- Never use `git commit --no-verify` or `-n`. It skips every commit check.
- Plan briefed work to end at a staged tree with the message in a file. The commit is your decision, in the yard.

### Step 6: review the branch from HELM

In HELM:

```bash
.venv/bin/python scripts/review-branch.py add-weekly-summary --intent-file <brief.md>
```

`--intent-file` is the brief or master instruction the yard worked from, so the review checks the work against what was asked. The script sends the branch's changes through CLIProxyAPI to an outside model and prints its findings. It only reports; it never merges anything.

| Exit code | Meaning |
|---|---|
| `0` | Nothing blocks the merge. |
| `1` | There are open bug or security findings. |
| `3` | The review failed or was cut short. Read the message. |

Send each blocking finding back to the yard with a new brief. A second run reviews only what changed since the last round. `--full` reviews the whole branch again. `--base` sets the branch to compare against; the default is `main`.

### Step 7: merge in HELM

In HELM:

1. Merge without committing yet:

   ```bash
   git merge --no-ff --no-commit add-weekly-summary
   ```

   `--no-ff` makes git always create a merge commit, and `--no-commit` stops before it so you can check first.
2. Run the same checks a push runs, on the merged tree:

   ```bash
   .venv/bin/python scripts/check-push-ratchets.py
   ```

   Exit `0` means pass. Exit `1` means one of the checks refused; the output names it. Fix it, or undo the merge with `git merge --abort`.
3. Commit the merge. If you use Lific, put one `closes <ID>` line per finished issue in the merge message.
4. If you use Lific, give that one merge message to it, so it marks those issues done:

   ```bash
   git log -1 --format=%B <merge-commit> | lific git-hook
   ```

   Never pass `--range` here. It replays every older `closes` line, and can close again an issue you reopened by hand.

### Step 8: delete the yard

Only after the merge. In HELM:

1. Find the yard's workspace id (it looks like `w12`): `herdr workspace list`.
2. Remove the checkout: `herdr worktree remove --workspace w12`.
3. If the workspace still shows in the sidebar: `herdr workspace close w12`.
4. Delete the branch: `git branch -d add-weekly-summary`.

When the checkout is removed, the plugin deletes the yard's scratch folder in your data repository, but only if the branch is merged into `main`. If it is not merged, the folder stays, and the plugin log says why.

## What a yard may do

- Read and change anything in its own folder.
- Write files into the data repository: reports, notes, drafts, memory files. HELM commits them.
- Read HELM with read-only commands such as `git log`, `cat`, `diff`, `ls`, `head` and `tail`.
- Run targeted tests and checks on its own folder.
- Commit on its own branch, when you type the word in it.
- In Lific: add a comment, or create a new issue. Nothing else.

## What a yard must never do

These are refused by the engine itself, mainly by the yard write guard in `.claude/hooks/_dispatch.py`, by `scripts/utils/clone_guard.py` and `scripts/lib/require-main-clone.sh` (which make HELM-only scripts stop in a yard), and by the data repository's commit hook:

- Change anything inside HELM.
- Read or change another yard's folder.
- Run git in the data repository: no `git add`, `git commit` or anything else there. Writing files there is fine.
- Publish anything, including any push. The yard's push address is set to `DISABLED://use-helm-to-publish`.
- Anything to do with a running background service. The rule is a category, not a list of verbs: from a yard, take no action after which a running process on the machine appears, disappears or behaves differently. Writing a service's source code in a yard is normal engine work. Running it is not.
- A cleanup or maintenance pass that writes across other tasks' files in the data repository.
- In Lific: change a status, link or delete issues, or edit another task's issue.

When a yard needs one of these done, it writes what HELM must do into its closing note and stops.

## Tests and the Fix-Note line

HEADING OS writes a new automated test for a change only when the change guards against one of two failures:

- **Failure A**: private data reaching the public engine.
- **Failure B**: the engine and the data repository merging into one, or a yard acting on something live.

Every other fix ships with no new test. Instead, its commit message carries one line:

```
Fix-Note: <what broke> | <what fixed it> | <why no test>
```

and the yard shows the fix works by running it. A commit check, `scripts/check-fix-note.py`, refuses a `fix:` or `emergency:` commit that touches `scripts/` or `.claude/hooks/` without a new test and without that line.

That is what the brief's tests line says: `Tests: none (Fix-Note)` for the ordinary case, or `Tests: failure A, <file>` / `Tests: failure B, <file>` naming the test file that holds the guarantee.

## Before a yard says it is ready

The yard runs these over everything it staged, not just the files it touched last:

1. The commit checks, over the whole staged set:

   ```bash
   pre-commit run --files $(git diff --cached --name-only)
   ```

2. The push checks, which commit checks do not cover:

   ```bash
   .venv/bin/python scripts/check-push-ratchets.py
   ```

   It writes nothing and works in a yard. Its last line goes in the closing note.
3. A security review of the branch, with the commit message file as the argument so the message is reviewed too:

   ```
   /security-review <message file>
   ```

   It compares the working tree with the point where the branch left your local `main`, so staged work that is not committed yet is included. Every high or medium finding is either fixed in the yard or listed in the closing note as not fixed.

## The closing note

When the yard finishes, it writes a short closing note. If you use Lific, the note is its closing comment on the issue. It says:

- what was done;
- **what was not done**, in its own paragraph, never left out silently;
- the commands it ran to check the work, and what they printed;
- the last line of `check-push-ratchets.py`;
- the `/security-review` result: the comparison point it used, how many findings, and each high or medium finding with its file and line;
- the `Unattended:` line that `.venv/bin/python scripts/checkpoint-paths.py --done "<note>"` prints, which says whether the yard ever carried on by itself at a pause;
- anything HELM must do that a yard may not, such as committing files in the data repository;
- as its last word, either `branch <name> ready for merge`, or, when the work produced no commit, that no merge is needed.

With Lific, the comment's first line follows the rules in `LIFIC.md`.

## Engine changes that come up while you work in HELM

HELM is the copy that is running. Fix an engine file directly in HELM when nothing running uses it: `.gitignore`, configuration, documentation, tests, new files, and scripts that no background service runs. Say so in the commit message.

Take the change to a yard when a half-finished edit would be live in HELM. Examples: `.claude/hooks/**` (loaded on every tool call), background service and timer sources, the `scripts/utils/` modules they import, and the guards. For an emergency, make the smallest fix in HELM, start the commit message with `emergency:`, and say so.

## Working without Herdr

Herdr is the supported way. If you cannot use it, you can make a yard by hand. These steps come from the plugin's own script header and were not run for this guide.

1. In HELM, create a worktree outside the engine folder: `git worktree add <yard folder> -b <branch>`.
2. In the yard folder, run the preparation by hand:

   ```bash
   FORCE_BOOTSTRAP=1 HERDR_PLUGIN_EVENT_JSON='{"worktree":{"path":"'"$PWD"'"}}' bash scripts/herdr/heading-os-yard/yard-bootstrap.sh
   ```

3. Check `.claude/.yard-bootstrap-status` reads `"status":"ok"` and `"step":11`.
4. Start Claude there with the marker: `HEADING_OS_YARD=1 claude --dangerously-skip-permissions`.

Without Herdr, `scripts/herdr-brief.py` cannot send briefs, so you paste the task into the yard's Claude yourself. Remove the yard with `git worktree remove <yard folder>` after the merge.

## Check that it works

1. In a fresh yard, `.claude/.yard-bootstrap-status` reads `"status":"ok"` and `"step":11`.
2. In the yard, ask Claude to write a file inside HELM. The yard write guard refuses it with a message that starts `YARD write guard`.
3. In the yard, `git remote get-url --push origin` prints `DISABLED://use-helm-to-publish`.
4. In HELM, `.venv/bin/python scripts/herdr-brief.py w12:p1 "$(cat brief.md)" --check` accepts a brief that has one tests line.
5. In the yard, `.venv/bin/python scripts/check-push-ratchets.py` runs and its last line reports the result.

## Common problems

**A commit in the yard is refused by the release check.** Your most recent typed message in that pane did not contain a commit word, or it contained a negation, or the message that authorised it was a brief. Type `commit` in the yard's pane yourself.

**A script says `this script runs from HELM (the main clone) only, not from a YARD worktree`.** That is correct behaviour. Run it in HELM, or write in the closing note that HELM must run it.

**A write is refused with `YARD write guard`.** The yard tried to write into HELM, into another yard, or into the data repository's git folder. Write into the yard's own folder, or write the data file through its registry name.

**A push from a yard fails with `DISABLED://use-helm-to-publish`.** Pushing from yards is switched off on purpose. Merge in HELM and push from there.

**The brief went to the wrong session (`herdr-brief.py` exit `4` with `IT LANDED IN`).** Tell the session that received it to ignore it. Find the right pane id with `herdr agent list` before sending again.

**The yard's Claude shows `YARD MARKER MISSING`.** It was started without `HEADING_OS_YARD=1`. Close it and start it again in the same pane with `HEADING_OS_YARD=1 claude --dangerously-skip-permissions`.

**`check-push-ratchets.py` exits `1` after a merge in HELM.** Do not commit the merge. Fix what it names, or run `git merge --abort` and send the finding back to the yard.

## Second opinions from other models

HEADING OS can ask outside models (Kimi, ChatGPT, Gemini, Grok) for an independent view, through CLIProxyAPI. Set that up with `CLIPROXY.md` first. These models are third-party services, so never send them private data.

- `/council <question>` asks them in parallel and shows their answers beside Claude's own. `/council --critique <draft>` has them stress-test a draft. The default asks Kimi only; `--gemini`, `--grok` and `--chatgpt` add one voice each, `--all` asks every voice, `--kimi-only` and the other `--<name>-only` flags ask exactly one, `--no-<name>` drops one, and `--no-log` saves no transcript.
- Each voice is also a script you can run yourself, for example:

  ```bash
  .venv/bin/python scripts/kimi-consult.py --mode independent --question "<your question>" --context "<facts it needs>"
  ```

  The same form works for `chatgpt-consult.py`, `gemini-consult.py` and `grok-consult.py`. `--mode critique --draft "<text>"` stress-tests a draft. The answer prints in the terminal.

## Updating and removing

There is nothing to install for yards, so there is nothing to update by hand. When you update the engine, the yard plugin's scripts update with it, because Herdr runs them from your engine folder. If an update changes the plugin's events, link it again, as `HERDR.md` shows.

To stop using yards, finish or abandon every open yard, merge what you want to keep, and remove each yard as in step 8. Then follow "Removing Herdr" in `HERDR.md` if you also want Herdr gone.
