#!/usr/bin/env python3
"""Commit and push BOTH HEADING OS repos to their private GitHub remotes.

The two-part topology has two git repos:
  - ENGINE: the workspace root clone (.heading-os)         -> origin/main
  - DATA  : the data overlay (get_data_root(), .heading-os-data) -> origin/main

This is the standing "always push both" routine. The DATA overlay goes FIRST: the
engine's pre-push hook runs the full suite inside the push, and data is the only
half that cannot be reconstructed. For each repo it:
  0. scans the CONTENT of everything about to be pushed for secrets, and for the
     engine also runs the routing and real-entity walls. All of this happens
     BEFORE the commit, so a secret is never written into local history;
  1. refuses to push if any file this push would CARRY -- tracked or merely
     untracked-and-not-ignored -- looks like a credential by NAME (.env,
     .session, cookies.json, .sessions/);
  2. asserts the rebuildable index (.memory-index/) is not tracked;
  3. commits staged changes (git add -A) unless --no-commit;
  4. checks the push PRECONDITIONS: the remote is one this repository may push
     to, the branch is main, and for an engine push the pre-push suite gate is
     armed;
  5. pushes origin main using GH_TOKEN from the engine .env AND verifies the
     branch is level with origin/main (ahead/behind == 0 0) as one step --
     a bare `git push` can report success yet leave the ref behind, so the
     ahead/behind check is the real gate.

Exit codes:
  0     everything that exists was pushed.
  3     everything that COULD be pushed was; at least one repo was skipped for a
        named reason (a branch that is not main, an unarmed engine test gate).
        Each skipped repo is still committed locally UNLESS --no-commit or
        --dry-run was passed, in which case its working-tree changes are still
        uncommitted; `_report_skips` prints which of the three it was, and this
        line claimed the first unconditionally until 2026-08-30. Read the
        headline, not the code: "Partial" means some repo did push, while "NOTHING
        PUSHED" means every repo was skipped and this run produced no off-machine
        copy at all. The pre-cutover mode pushes one repo, so exit 3 there
        is always the second shape.
  1, 2  a failure that stops the run: a security refusal, a remote a repository
        must not push to, an absent push token, a misconfigured data root, or a
        push that ran and did not verify.

A refusal about ONE repository never cancels another. See RepoNotPushable.

Usage:
  python scripts/push-all.py [-m "commit message"] [--no-commit] [--dry-run]
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Re-exec under the project venv before anything else: the test gate spawns its
# runner with sys.executable, so the whole chain must inherit the locked deps
# (the system interpreter lacks pytest-cov). See scripts/utils/venv_guard.py.
from scripts.utils.venv_guard import ensure_venv  # noqa: E402

ensure_venv()

from scripts.utils.clone_guard import require_main_clone
from scripts.utils.colors import BOLD, CYAN, GRAY, GREEN, RED, RESET, YELLOW
from scripts.utils.content_denylist import build_denylist
from scripts.utils.denial_log import CONTEXT_ENV, log_denial
from scripts.utils.engine_guard import (
    engine_text_files,
    engine_text_rels,
    repo_carried_paths,
    scan_engine_repo,
)
from scripts.utils.git_push import ahead_behind, remote_objection, supervised_push
from scripts.utils.data_path import DataPathError, data_write_dir
from scripts.utils.push_ratchets import (
    DATA_CONFIG_PATHS,
    DATA_PATH,
    ENGINE_DATA_PATHS,
    ENGINE_DIR_WRITES,
    ENGINE_ONLY,
    IMPORT_ENV_PURITY,
    LEGACY_OUTPUTS,
    OUTSIDE_WORKSPACE_WRITES,
    RUNTIME_WRITE_GUARD,
    SKILL_DATA_PATHS,
    run_ratchet,
    scanner_path,
)
from scripts.utils.push_history import (
    HistoryUnavailable,
    generations,
    read_blob,
    published_bytes,
    published_engine_paths,
    unpushed_blobs,
    unpushed_paths,
)
from scripts.utils.workspace import (
    get_default_tz,
    get_data_root,
    get_workspace_root,
    load_env,
)

logger = logging.getLogger(__name__)

# Tracked-path patterns that must never reach a remote. The .gitignore already
# excludes these; this is the belt-and-braces code check.
SECRET_TRACKED = re.compile(
    r"(^|/)\.env$|(^|/)\.env\.|\.session$|(^|/)\.sessions/|(^|/)cookies\.json$"
)

# Authoritative content scan. The pre-commit hook is an early-catch layer that
# `git commit --no-verify` can skip; this scan runs in pure code on the push
# path, so a bypassed commit is still caught before anything leaves the
# machine. There is no flag to skip it here, and a plain `git push` runs the
# same walls through the pre-push hook (`pre_push_scan` below). Only
# `git push --no-verify`, which the security rule forbids, skips that hook.
SCANNER = Path(__file__).resolve().parent / "secret-scanner.py"

# A bound against a STALLED scanner, never a performance target. MEASURED
# 2026-09-18: a 3,734 MB delta took 262.3 s on a quiet machine, and a 300 s
# bound refused the backup twice under load. Raised rather than capping what
# is read, because an unread large file is what a content wall must not allow.
SCANNER_TIMEOUT_S = 1800


class RepoNotPushable(Exception):
    """This repository cannot be pushed right now. Says nothing about the others.

    The distinction this type exists to make is the whole point of the change that
    introduced it. push-all.py had ten sys.exit sites and they were two different
    statements wearing one uniform:

      - STOP THE WORLD: a secret in content, a data-class artifact in the engine
        clone, a real-entity token in an engine-routed file, a secret-like tracked
        filename, an absent push token, a misconfigured data root. Something is
        wrong that the operator must see before anything leaves the machine.
      - THIS REPO CANNOT BE PUSHED: a branch that is not main, an engine whose
        pre-push gate is not armed. These say nothing whatever about the other
        repository.

    Treating the second kind as the first meant that whenever the engine clone sat
    on a feature branch, the process died at the engine and the DATA overlay was
    never pushed at all. The engine sits on a feature branch during every slice of
    work by construction, so the backup was declining to back up the only
    irreplaceable half of the workspace for the duration of every slice. Measured
    twice in one session on 2026-07-29 and hand-worked-around both times.

    Raise this for a per-repository refusal. Keep sys.exit for the other kind. A
    new gate's author has to choose, and the choice is visible at the call site
    rather than resting on a convention nobody reads.
    """


def _z_paths(args: list[str], repo: Path) -> list[str]:
    """NUL-separated git path output as real names, decoded from BYTES.

    The shared `run` helper below is text mode, which is correct for the commit
    messages and refs it mostly carries and wrong for filenames: text mode turns
    on universal newlines and rewrites every CR byte to LF. Separate helper
    rather than a flag on `run`, so the choice is visible at the call site and a
    new path reader has one obvious thing to copy.
    """
    out = subprocess.run(
        args, cwd=str(repo), check=True, capture_output=True,
    ).stdout.decode("utf-8", "surrogateescape")
    # No `.strip()`: a filename may legally begin or end with whitespace.
    return [path for path in out.split("\0") if path]


def _push_delta_files(repo: Path, *, will_commit: bool = True) -> set[str]:
    """Files about to be pushed: the committed-but-unpushed delta, staged and
    unstaged tracked edits, and every untracked file git is not ignoring.

    `will_commit=False` is `--no-commit`, and it drops the last three legs. They
    are here because step 3 runs `git add -A`, so a file merely sitting in the
    worktree at scan time is inside the commit a moment later; `--no-commit`
    runs no staging step at all, and a file that is not already in a commit
    cannot reach the remote on that invocation. Scanning it there is not extra
    safety, it is a refusal over bytes the run would not carry.

    MEASURED 2026-09-05: a `push-all.py` the operator asked for refused over two
    UNTRACKED files belonging to another task, with `--no-commit --dry-run`
    giving the identical findings. The narrowing is safe BECAUSE of the staging
    command, not because untracked files are harmless: add a staging step to
    the `--no-commit` path and it becomes a hole, silently.

    The untracked leg was missing until 2026-08-23, and the gap was structural
    rather than incidental. `engine_content_scan` runs at step 0 of `push_repo`,
    deliberately BEFORE the commit, so that a tree staged with `--no-verify`
    cannot slip past. But `git diff` sees only tracked files, so a brand-new file
    was invisible at scan time and committed by `git add -A` a moment later. The
    routing wall next to it has always used `git ls-files --others`, and the two
    have to agree about what "about to be pushed" means.

    Ignored files stay out: they are not going to be pushed, and scanning them
    would refuse a push over the contents of `.sessions/` or a scratch file.

    Every command runs with `-z`. Without it git C-quotes any path holding a
    non-ASCII byte, and the quoted string matches no routing rule and opens no
    file - so a Cyrillic-named artifact walked through the content walls on a
    workspace whose operator writes in Russian. Same defect, same day, as
    `engine_guard.repo_carried_paths`.

    `_z_paths`, not the shared `run`, for the reason recorded there on
    2026-08-30: `run` is text mode, every subprocess text mode turns on universal
    newlines, and that rewrites each CR byte to LF with no `newline=` knob to
    switch it off. `-z` is no defence, because the translation happens in Python
    after git has already emitted the bytes verbatim. These paths feed
    `_run_scanner` and `engine_content_scan`, which open them by name, so a
    mistranslated name is a file about to be pushed that no scanner ever reads.
    """
    return set().union(*_push_delta_legs(repo, will_commit=will_commit).values(),
                       set())


def _push_delta_legs(repo: Path, *, will_commit: bool = True
                     ) -> "dict[str, set[str]]":
    """The same scan set as `_push_delta_files`, kept apart by GIT STATE.

    One source for both, on purpose. The refusal message names the composition
    of the scan set (`_scan_set_composition`), and an earlier version of that
    message re-ran these same git commands from a second copy of the argv. Two
    copies of `--diff-filter=ACMT` is this repository's dominant defect shape:
    the day someone widens the filter here, the other copy keeps describing the
    old one and the description quietly stops matching the scan.

    It also stopped a sibling check dead. That check replaced `subprocess.run`
    wholesale with a double asserting BYTES mode, because the scanner handoff
    is bytes; the second copy's text-mode git calls went through that double
    and tripped an assertion written about a different call. Deriving the
    composition from the legs already collected means the refusal path runs no
    subprocess at all. The check went with the 2026-09-21 test-suite split;
    keeping one copy of the argv is still the reason this function exists.
    """
    have_base = run(
        ["git", "rev-parse", "--verify", "-q", "origin/main"], repo, check=False
    ).returncode == 0
    # `origin/main..HEAD` needs BOTH ends to resolve, and `have_base` proves only
    # the left one. On an unborn HEAD git exits 128, `run` defaults to
    # check=True, and the resulting CalledProcessError is not one of the two
    # things `_attempt` absorbs -- so the whole backup died with a traceback and
    # NEITHER repo was pushed. `engine_content_scan` calls this at step 0, before
    # the commit, which is where a fresh clone with a remote but no commits of
    # its own meets it on an ordinary run.
    have_head = run(
        ["git", "rev-parse", "--verify", "-q", "HEAD"], repo, check=False
    ).returncode == 0
    legs_by_state: dict[str, set[str]] = {}
    if have_base and have_head:
        # `--no-renames`, for the reason `push_history.unpushed_blobs`
        # already gives it. With rename detection on (the git default) a
        # `git mv` plus an edit is ONE `R` entry, and `--diff-filter=ACM`
        # drops it, so the DESTINATION path appears in no leg at all.
        # MEASURED 2026-08-29 in a scratch repo with a real bare remote: a
        # staged rename carrying a new secret returned the EMPTY set here,
        # `content_scan` skipped the scanner because `if files:` was False,
        # `engine_content_scan` opened no file, and step 3 committed and
        # step 5 pushed the token. Only the bypassable pre-commit hook
        # stood in the way, which is exactly what this wall backstops.
        # Turning renames off restores the `A` for the destination path.
        #
        # `T` (typechange) joined `ACM` on 2026-09-02, for the same reason one
        # letter further along. A tracked regular file replaced by a symlink
        # is ONE `T` entry and nothing else, so the path fell out of every leg
        # and this wall never opened it. MEASURED that day in a scratch repo:
        # `git diff --name-status` reported `T f.txt` while
        # `--diff-filter=ACM` returned the empty set and `--diff-filter=ACMT`
        # returned `f.txt`. The workspace forbids symlinks, which is why the
        # gap was rated unreachable; a content wall that relies on another
        # rule holding is a wall with a hole in it, and widening a filter can
        # only ever add files to the scan.
        legs = [
            ("committed-unpushed",
             ["git", "diff", "-z", "--no-renames", "--name-only",
              "--diff-filter=ACMT", "origin/main..HEAD"]),
        ]
        if will_commit:
            legs += [
                ("staged",
                 ["git", "diff", "-z", "--cached", "--no-renames", "--name-only",
                  "--diff-filter=ACMT"]),
                ("unstaged",
                 ["git", "diff", "-z", "--no-renames", "--name-only",
                  "--diff-filter=ACMT"]),
            ]
        for state, args in legs:
            legs_by_state[state] = set(_z_paths(args, repo))
    elif not will_commit and have_head:
        # No `origin/main`: the push carries the whole history, so the delta is
        # everything COMMITTED. Tracked-only, because nothing will be staged.
        legs_by_state["tracked (no origin/main: the push carries the whole "
                      "history)"] = set(_z_paths(["git", "ls-files", "-z"], repo))
    elif not will_commit:
        # No HEAD and nothing to commit: this invocation can push nothing.
        return {}
    else:
        # No base, or no HEAD: the index IS the whole delta. `git ls-files`
        # works against an unborn HEAD and lists everything staged, so this
        # branch loses no coverage in either case.
        legs_by_state["staged"] = set(_z_paths(["git", "ls-files", "-z"], repo))
    if will_commit:
        legs_by_state["untracked"] = set(
            _z_paths(["git", "ls-files", "-z", "--others", "--exclude-standard"],
                     repo)
        )
    return {state: {f for f in paths if f}
            for state, paths in legs_by_state.items()}


def _run_scanner(paths, cwd: Path, context: str, extra_env=None):
    """Run secret-scanner.py over `paths`, relative to `cwd`. Exits 2 on a stall.

    The scanner counts its own refusal; `context` names the caller so a
    push-time catch is distinguishable from a commit-time one. Counting here as
    well would record one refusal twice and corrupt the denominator.

    The repo name rides along because the scanner records a repo-RELATIVE path
    and the callers run over both clones: without it, the same relative path in
    the engine and in the data overlay produce two records nothing can tell
    apart, which is the ambiguity the context field exists to remove.

    `--stdin0` and BYTES, not `--stdin` and text. `_z_paths` goes to the
    trouble of reading git's `-z` output as bytes so a path holding a newline
    survives verbatim, and this handoff then joined that list with `"\n"` and
    handed it to a reader that splits on `"\n"`. MEASURED 2026-09-01 in a
    scratch repo: a tracked `two\nlines.env` carrying a `ghp_`-shaped token
    arrived at the scanner as two names that open nothing, both skipped in
    silence, and `content_scan` passed the push. The identical token in
    `creds.env` was refused, so the measurement was measuring something. Text
    mode also cannot encode a surrogateescape name at all, which is the other
    half of what `_z_paths` preserves.
    """
    env = dict(os.environ, **{CONTEXT_ENV: context})
    if extra_env:
        env.update(extra_env)
    payload = b"\0".join(p.encode("utf-8", "surrogateescape") for p in sorted(paths))
    try:
        proc = subprocess.run(
            [sys.executable, str(SCANNER), "--stdin0"],
            cwd=str(cwd), input=payload,
            capture_output=True, env=env, timeout=SCANNER_TIMEOUT_S,
        )
        # Decoded here rather than by `text=True`, so the INPUT stays bytes.
        # `_refuse_on_scanner` writes these straight to the caller's streams.
        return subprocess.CompletedProcess(
            proc.args, proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"),
        )
    except subprocess.TimeoutExpired:
        # Bounded for the same reason the pushes are: an indefinite stall in the
        # only irreplaceable-half backup path is worse than a named failure.
        print(f"{RED}REFUSING TO PUSH -- secret scanner exceeded "
              f"{SCANNER_TIMEOUT_S}s in {cwd}.{RESET}")
        sys.exit(2)


def _refuse_on_scanner(proc, where: str, composition: str = "") -> None:
    if proc.returncode != 0:
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        reason = f"secret-like CONTENT in {where}" if proc.returncode == 1 \
            else "secret-scanner error"
        print(f"{RED}REFUSING TO PUSH — {reason}.{RESET}")
        if composition:
            print(composition)
        sys.exit(2)


def _scan_set_composition(legs: "dict[str, set[str]]", *,
                          will_commit: bool) -> str:
    """One line naming what the refused scan set was made of, by git state.

    `.claude/rules/scope-claims.md`, in the wall that rule most protects. The
    refusal above names a FILE and asserts it is "about to be pushed". In the
    default mode that is true of all four legs, because step 3 runs
    `git add -A`; the operator still cannot tell from the sentence whether the
    offending bytes are already in a commit or are an untracked scratch file
    that this run is about to sweep in, and those call for different responses.

    MEASURED 2026-09-05: a refusal named two untracked files under another
    task's output directory and said only "a file about to be pushed", and
    reading the state took three git commands by hand while the push was
    blocked. Counts, not paths, because the scanner has already printed the
    paths that matter and a second full listing buries them.

    Takes the legs `_push_delta_legs` already collected rather than asking git
    again. Nothing here runs a subprocess: the counts describe the set that was
    actually scanned, not a second walk that could disagree with it.
    """
    parts = [f"{len(paths)} {state}" for state, paths in legs.items()]
    if will_commit:
        tail = ("the last three legs are here because step 3 runs `git add -A`, "
                "so they are in the commit a moment later")
    else:
        tail = ("--no-commit stages nothing, so the committed delta is the whole "
                "set this run can push")
    return f"  scanned set: {', '.join(parts)} — {tail}."


def content_scan(repo: Path, *, will_commit: bool = True) -> None:
    """Scan the contents of everything about to be pushed. Refuse on any hit.

    TWO WORLDS, because a push sends both. The working tree and index are what
    the next commit will carry; the unpushed COMMITS are what the wire will
    carry regardless of what the disk says now. Scanning only the first was the
    defect, and it is the defect this docstring used to deny by claiming "a
    bypassed commit is still caught before anything leaves the machine".

    MEASURED 2026-08-29 on a real repository with a real bare remote
    (`.tmp/audit/measure61.py`), before the history pass existed:

      secret committed with `--no-verify`, then wiped from the working tree
        -> the file WAS listed, the scanner read the cleaned bytes off the disk,
           "No secrets detected.", exit 0, push ships the commit.
      commit A adds the secret, commit B removes it, both unpushed
        -> the two-endpoint diff nets to nothing, the file was not even listed,
           exit 0, push ships commit A.
      control, the same secret sitting in the working tree
        -> refused, so the measurement was measuring something.

    Both directions are now covered and neither replaces the other: history says
    nothing about an uncommitted edit, and the working tree says nothing about a
    commit already made.
    """
    legs = _push_delta_legs(repo, will_commit=will_commit)
    files = set().union(*legs.values(), set())
    if files:
        _refuse_on_scanner(
            _run_scanner(files, repo, f"push:{repo.name}"),
            "a file about to be pushed",
            _scan_set_composition(legs, will_commit=will_commit))
    history_content_scan(repo)


def history_content_scan(repo: Path) -> None:
    """Scan the BYTES of every unpushed commit, not the bytes on disk.

    The blobs are laid out in a scratch tree because the scanner takes file
    paths, and they are laid out in GENERATIONS because it cannot be handed two
    versions of one path at once. See `push_history.generations`: the pass count
    is the largest number of versions any single file has in the range, not the
    commit count.

    Two environment variables, and both are load-bearing:

    `WORKSPACE_ROOT` points at the scratch tree so the scanner's own
    `SKIP_PATHS` resolve. Those three self-referencing files -- the scanner,
    `secret_patterns.py`, `.env.example` -- contain secret patterns by
    definition, and a commit that edits one of them is ordinary here. Without
    this the history pass would refuse the backup over the scanner's own source.
    Re-listing those paths locally was the alternative and it is the duplication
    this audit keeps finding: the second copy is the one that stops being fixed.

    `HEADING_OS_DATA` pins the denial log back to the REAL data repository.
    The child's `WORKSPACE_ROOT` is the scratch tree, and a data root discovered
    beside a scratch tree is no data root, so without the pin each refusal would
    go unrecorded -- a gate that refuses and keeps no record of having refused.
    It pinned `WORKSPACE_LOG_DIR` until 2026-09-25, when the denial log left the
    engine checkout's `.logs/` for the data repository (HOS-5 section 7).
    """
    try:
        blobs = unpushed_blobs(repo)
    except HistoryUnavailable as exc:
        # Unverified is not clean, and this branch means git could not say what
        # the push would send.
        print(f"{RED}REFUSING TO PUSH — cannot read the unpushed history of "
              f"{repo.name}: {exc}{RESET}")
        sys.exit(2)
    if not blobs:
        return

    real_data_root = str(get_data_root())
    with tempfile.TemporaryDirectory(prefix="heading-push-history-",
                                     dir=data_write_dir("runtime:scratch/tmp")) as scratch:
        for index, group in enumerate(generations(blobs)):
            root = Path(scratch) / str(index)
            rels: list[str] = []
            for blob in group:
                dest = (root / blob.rel).resolve()
                # git tree entries cannot hold `..`, so this is belt and braces
                # rather than a live hole -- and it is cheap insurance on the
                # one path in this file that writes attacker-influenced names.
                if not dest.is_relative_to(root.resolve()):
                    print(f"{RED}REFUSING TO PUSH — a committed path escapes "
                          f"its own tree: {blob.rel}{RESET}")
                    sys.exit(2)
                dest.parent.mkdir(parents=True, exist_ok=True)
                # write_bytes, so a blob whose mode is a symlink lands as an
                # ORDINARY FILE holding the target string. The workspace creates
                # no symlinks, and materialising one here would let a committed
                # link point the scanner at something outside the scratch tree.
                try:
                    dest.write_bytes(read_blob(repo, blob.sha))
                except HistoryUnavailable as exc:
                    print(f"{RED}REFUSING TO PUSH — {exc}{RESET}")
                    sys.exit(2)
                rels.append(blob.rel)
            proc = _run_scanner(
                rels, root, f"push:{repo.name}:history",
                extra_env={"WORKSPACE_ROOT": str(root),
                           "HEADING_OS_DATA": real_data_root})
            if proc.returncode != 0:
                print(f"{YELLOW}The paths below are the versions carried by the "
                      f"UNPUSHED COMMITS of {repo.name}, not the files on disk. "
                      f"Cleaning the working tree does not remove them: the "
                      f"history has to be rewritten.{RESET}")
            _refuse_on_scanner(proc, "a commit about to be pushed")


def engine_clean_scan(repo: Path) -> None:
    """Authoritative, UNBYPASSABLE engine/data leak gate. Refuse the push if the
    engine clone carries ANY file routing private.

    This is the routing-destination sibling of content_scan(): pure code on the
    sanctioned push path, no skip flag. The pre-commit `engine-tree-clean` hook and
    the pre-push test suite assert the same invariant earlier, but both are
    bypassable (`git commit --no-verify`, an un-armed/removed pre-push hook). This
    wall is not -- a data artifact in the engine tree cannot leave the machine
    regardless of how it got committed. Added 2026-06-22 after a `docs/superpowers/`
    leak survived precisely because the routing check ran only at bypassable layers.

    The unpushed HISTORY is judged alongside the working tree, and the reason is
    the same 2026-06-22 leak in the shape it would take today: commit the
    private-routed file, notice, `git rm` it, commit again, push. Both commits go
    to the remote; `git ls-files` and `--others` have nothing left to report; the
    wall clears a push that ships the file. A working-tree scan cannot see a
    deletion, because a deletion is what it looks like from there.

    A history path the remote ALREADY holds as engine is excused
    (`published_engine_paths`), and only a history path: otherwise every file
    that leaves the engine, taking its `engine` rule with it, makes any earlier
    unpushed edit to it unpushable. A path new to the remote is judged by the
    current map exactly as before, and `engine_content_scan` still reads the
    excused blobs.
    """
    try:
        carried_history = unpushed_paths(repo)
        published = published_engine_paths(repo)
    except HistoryUnavailable as exc:
        print(f"{RED}REFUSING TO PUSH — cannot read the unpushed history of "
              f"{repo.name}: {exc}{RESET}")
        sys.exit(2)
    flagged = dict.fromkeys(scan_engine_repo(repo, extra_paths=carried_history,
                                             published=published))
    if flagged:
        # A path can be flagged for either reason or both, and the two need
        # different remedies, so the report says which. Saying "git rm --cached"
        # over a path that is already deleted on disk sends the operator to do
        # something that changes nothing and leaves the leak in the history.
        on_disk = {rel for rel in flagged if (repo / rel).exists()}
        for rel in flagged:
            log_denial(mechanism="push:engine-clean-scan", action="push",
                       path=rel, reason="routes private in the engine clone")
        print(f"{RED}REFUSING TO PUSH — data-class artifact(s) in the engine clone:{RESET}")
        for f in flagged:
            where = "working tree" if f in on_disk else "unpushed history"
            print(f"  {RED}{f}{RESET} {GRAY}[{where}]{RESET}")
        print(f"{GRAY}The engine repo is code only. These route private and "
              f"belong in the DATA root (.heading-os-data).{RESET}")
        if on_disk:
            print(f"{GRAY}Working tree: move them out (git rm --cached) and add the "
                  f"path to .gitignore.{RESET}")
        if len(flagged) > len(on_disk):
            print(f"{GRAY}Unpushed history: deleting the file is not enough, the "
                  f"commits still carry it. Rewrite the range before pushing.{RESET}")
        sys.exit(2)


def _refuse_ratchet(refusal: str, output: str, hint: str = "") -> None:
    """Print a ratchet's refusal and stop the push, as one call that cannot be split.

    HOS-156, MEASURED 2026-09-28: `outside_workspace_writes_ratchet` printed its
    refusal and then returned, because it was the one ratchet of six whose
    `sys.exit(2)` had been left off, and push-all went on to push the engine.
    Each ratchet used to carry its own print-then-exit, so the exit was a line
    anyone could drop; here the words and the stop are the same call.
    """
    print(f"{RED}REFUSING TO PUSH{refusal}{RESET}")
    print(output.rstrip())
    if hint:
        print(f"{GRAY}{hint}{RESET}")
    sys.exit(2)


# The argv of the nine ratchets below lives in `scripts/utils/push_ratchets.py`, and
# `scripts/check-push-ratchets.py` runs the same table on any checkout, so a
# refusal can be seen before the merge rather than here (HOS-125). Change what a
# ratchet runs THERE, never by giving one of these functions its own argv again.
def data_path_ratchet(repo: Path) -> None:
    """Refuse the push when the engine names MORE unresolvable data paths than before.

    HOS-5's progress is measured by `scripts/check-data-path-references.py`, and
    until 2026-09-19 that scanner ran nowhere automatically: MEASURED that day,
    `.pre-commit-config.yaml` did not mention it, no CI step invoked it, and no
    push gate called it. The instrument that decides whether the refactoring is
    advancing only ran when somebody remembered to type it.

    It is wired HERE rather than into pre-commit, and the reason is mechanical
    rather than stylistic. The scanner refuses with exit 2 on any tree without
    the private overlay ("it has nothing to say on a public clone"), the engine
    repo is public, and its setup instructions tell every fresh clone to run
    `pre-commit install`. A hook entry would have failed the first commit anyone
    made in a clone that has no private data, which is every clone but this one.
    Push and `/backup` happen only in HELM, where the overlay always exists, so
    the ratchet loses no coverage by living on the push path.

    `--skip-without-overlay` is still passed, because "runs only in HELM" is a
    statement about today's practice and not a property anything enforces, and a
    gate that fails closed on the absence of private data is a gate that gets
    disabled.

    This refuses on a RISE, never on the debt itself. The figure in this
    paragraph was 187 and it is a measurement of a moving tree, so re-derive it
    with `python scripts/check-data-path-references.py --json` rather than
    trusting the digits: MEASURED 2026-09-21 after the `outputs` move, it was
    107 against a ceiling of 183. None of it is intentional, so the honest
    instrument is a count that only falls. BASELINE, the scanner's list of paths
    that SHOULD not resolve, stays empty for the reason frozen beside it on
    2026-09-12: filling it would hide the list the migration has to work
    through.

    **A ratchet and a prohibition are passed, not one.** `--pins-check` is HOS-5
    section 6: every statement in engine Python that SPELLS an address in the
    Data Repository must carry a reviewed entry, with a reason, in
    `config/data-path-pins-exemptions.json`, and every entry there must still
    match a statement. It replaced a count that only refused growth, on
    2026-09-25. It is a different question from `--ratchet`, which asks whether
    the addresses the engine names still resolve; a tree can hold that one flat
    and acquire new hard-coded addresses all the same. Both are passed in one
    invocation because the scan is the expensive part and it is the same scan.
    """
    proc = run_ratchet(repo, DATA_PATH)
    if proc is None:
        # A clone without the scanner is not a clone with clean data paths, but
        # neither is it one this gate can judge. Say so; never pass silently.
        print(f"{YELLOW}data-path ratchet skipped: "
              f"{scanner_path(repo, DATA_PATH)} is absent.{RESET}")
        return
    if proc.returncode == 0:
        tail = [ln for ln in proc.stderr.splitlines()
                if "ratchet" in ln or "section 6" in ln]
        for line in tail[-2:]:
            print(f"{GRAY}{line}{RESET}")
        return
    _refuse_ratchet(
        " — the engine names more unresolvable data paths than the recorded "
        "floor, or spells a data address no reviewed exemption covers.",
        proc.stderr,
        "Fix the reference. The unresolved-path floor is lowered with "
        "`--write-ratchet` once that count has genuinely fallen; a "
        "hard-coded address becomes `data_path(\"<name>\")`, or an entry "
        "with its reason in config/data-path-pins-exemptions.json.")


def legacy_outputs_ratchet(repo: Path) -> None:
    """Refuse the push when MORE engine places address the drained `outputs` tree.

    `scripts/check-legacy-outputs-sites.py` arrived with HOS-76 and ran nowhere
    automatically. It is wired HERE and deliberately NOT into `.pre-commit-config.yaml`,
    and the reason is the test budget rather than taste. Registering it as a hook
    makes `scripts/check-gate-integrity.py` require a test naming it, and
    `config/test-budget.json` stands exactly at its ceiling of 17 test files, so
    the registration costs a declared raise. HOS-54 says a test exists for two
    failures only, a private byte reaching the public engine and the two
    repositories merging; an address inside the private overlay is neither. So a
    hook entry would spend a budget raise on a test the policy says not to write.

    Its sibling above answers the same question the same way, and says so in its
    own docstring: push and `/backup` happen only in HELM, so the push path loses
    no coverage. The cost, stated rather than left to be discovered: a commit that
    raises the count sits in local history until the next push. For an address
    inside the private overlay that is harmless, and it is the moment before
    anything leaves the machine that matters for a public repository.

    Operator decision 2026-09-22, choosing this over the pre-commit wall he was
    offered.
    """
    proc = run_ratchet(repo, LEGACY_OUTPUTS)
    if proc is None:
        print(f"{YELLOW}legacy-outputs ratchet skipped: "
              f"{scanner_path(repo, LEGACY_OUTPUTS)} is absent.{RESET}")
        return
    if proc.returncode == 0:
        tail = [ln for ln in (proc.stdout + proc.stderr).splitlines()
                if "ceiling" in ln or "site" in ln]
        for line in tail[-2:]:
            print(f"{GRAY}{line}{RESET}")
        return
    _refuse_ratchet(
        " — more engine places address the drained legacy outputs tree than the "
        "recorded floor, or a REVIEWED row went stale.",
        proc.stdout + proc.stderr,
        "Resolve the name at the new site, or update the REVIEWED table with "
        "the verdict and its reason. The count may only fall.")


def import_env_purity_ratchet(repo: Path) -> None:
    """Refuse the push when importing an engine module would write the environment.

    `scripts/dev/import-env-purity.py` arrived with HOS-95, after a module-level
    `load_env()` repointed the data root of every process that imported it. It
    ran nowhere until HOS-116. It is wired HERE and not into
    `.pre-commit-config.yaml` for the reason `legacy_outputs_ratchet` above
    gives: a hook makes `scripts/check-gate-integrity.py` require a test, and
    HOS-54 does not call for one here. The same cost follows: a commit that adds
    a write sits in local history until the next push.

    It is not a count that may only fall. Every finding must be exempt, by the
    checker's one rule for `ensure_venv()` in a CLI script nothing imports by
    name, or by a reviewed entry in `config/import-env-purity-exemptions.json`.
    An entry that matches nothing refuses too. Operator decision 2026-09-25.
    """
    proc = run_ratchet(repo, IMPORT_ENV_PURITY)
    if proc is None:
        print(f"{YELLOW}import-env-purity ratchet skipped: "
              f"{scanner_path(repo, IMPORT_ENV_PURITY)} is absent.{RESET}")
        return
    if proc.returncode == 0:
        tail = [ln for ln in (proc.stdout + proc.stderr).splitlines()
                if "findings" in ln or "exempt" in ln]
        for line in tail[-2:]:
            print(f"{GRAY}{line}{RESET}")
        return
    _refuse_ratchet(
        ": importing an engine module would change the process environment, "
        "with no rule or reviewed entry exempting it, or an exemption entry "
        "went stale.",
        proc.stdout + proc.stderr,
        "Move the write into a function, or record why it is intended in "
        "config/import-env-purity-exemptions.json.")


def outside_workspace_writes_ratchet(repo: Path) -> None:
    """Refuse the push when engine code would write a Workspace artifact outside it.

    `scripts/dev/outside-workspace-writes.py` arrived with HOS-5 section 7
    (defect 5, 2026-09-25), after 20 `tempfile` calls were found putting their
    files in the system temp directory, a router password and a copy of the
    browser cookie store among them. Wired HERE and not into pre-commit for the
    reason `import_env_purity_ratchet` gives. Every finding must be covered by
    a reviewed entry in `config/outside-workspace-writes-exemptions.json`, and
    an entry that matches nothing refuses too.
    """
    proc = run_ratchet(repo, OUTSIDE_WORKSPACE_WRITES)
    if proc is None:
        print(f"{YELLOW}outside-workspace-writes ratchet skipped: "
              f"{scanner_path(repo, OUTSIDE_WORKSPACE_WRITES)} is absent.{RESET}")
        return
    if proc.returncode == 0:
        tail = [ln for ln in (proc.stdout + proc.stderr).splitlines() if "searched" in ln]
        for line in tail[-1:]:
            print(f"{GRAY}{line}{RESET}")
        return
    _refuse_ratchet(
        ": engine code writes a Workspace artifact outside the Workspace with "
        "no reviewed entry exempting it, or an exemption entry went stale.",
        proc.stdout + proc.stderr,
        "Give the write a Workspace directory, or record why the "
        "statement writes nothing in "
        "config/outside-workspace-writes-exemptions.json.")


def engine_dir_writes_ratchet(repo: Path) -> None:
    """Refuse the push when the engine spells an address inside its own checkout
    that git ignores, with no reviewed entry saying why.

    HOS-5 section 7, 2026-09-25: the engine had been writing logs, scratch, hook
    state, caches and daemon pid files into gitignored directories of its own
    checkout, where every wall that selects over `git ls-files` plus the
    untracked-and-not-ignored set is blind by construction. Those writers moved
    to the data repository that day; `scripts/dev/engine-dir-writes.py` keeps a
    new one from appearing. It is wired HERE and not into `.pre-commit-config.yaml`
    for the reason `legacy_outputs_ratchet` gives: a hook makes
    `scripts/check-gate-integrity.py` require a test, and HOS-54 does not call
    for one. The same cost follows: a commit that adds such an address sits in
    local history until the next push.
    """
    proc = run_ratchet(repo, ENGINE_DIR_WRITES)
    if proc is None:
        print(f"{YELLOW}engine-dir-writes ratchet skipped: "
              f"{scanner_path(repo, ENGINE_DIR_WRITES)} is absent.{RESET}")
        return
    if proc.returncode == 0:
        tail = [ln for ln in (proc.stdout + proc.stderr).splitlines()
                if "finding" in ln]
        for line in tail[-1:]:
            print(f"{GRAY}{line}{RESET}")
        return
    _refuse_ratchet(
        ": the engine spells an address inside its own checkout that git "
        "ignores, with no reviewed entry covering it, or an entry went stale.",
        proc.stdout + proc.stderr,
        "Write it to the data repository through "
        "`data_write_dir(\"runtime:<service>/<kind>\")`, or record why it "
        "belongs in the checkout in config/engine-dir-writes-exemptions.json.")


def skill_data_paths_ratchet(repo: Path) -> None:
    """Refuse the push when MORE skill lines name a data address by its physical path.

    The data repository's folders are about to move, and the move is meant to
    change its `config/data-paths.yaml` and no skill. A skill that spells
    the contacts directory's physical path works today only through
    `.claude/hooks/data-path-redirect.py`, and after the move that redirect
    lands on a folder that no longer holds the records.
    `scripts/audit-skill-data-paths.py` counts those lines against a ceiling
    that may only fall. Wired HERE and not into `.pre-commit-config.yaml` for
    the reason `legacy_outputs_ratchet` gives.
    """
    proc = run_ratchet(repo, SKILL_DATA_PATHS)
    if proc is None:
        print(f"{YELLOW}skill-data-paths ratchet skipped: "
              f"{scanner_path(repo, SKILL_DATA_PATHS)} is absent.{RESET}")
        return
    if proc.returncode == 0:
        tail = [ln for ln in (proc.stdout + proc.stderr).splitlines()
                if "ceiling" in ln]
        for line in tail[-1:]:
            print(f"{GRAY}{line}{RESET}")
        return
    _refuse_ratchet(
        ": a skill names a data-repository address by its physical path beyond "
        "the recorded ceiling, the ceiling is above the tree, or a reviewed "
        "entry went stale.",
        proc.stdout + proc.stderr,
        "Resolve the address through `python scripts/data-path.py path <name>`, "
        "or record a reviewed entry in config/skill-data-paths-exemptions.json. "
        "The ceiling may only fall.")


def engine_data_paths_ratchet(repo: Path) -> None:
    """Refuse the push when MORE engine-code lines name a data address by its physical path.

    HOS-234, operator decision 2026-10-13: no hard-coded data paths anywhere,
    and everything new follows that. `scripts/audit-skill-data-paths.py
    --engine` asks `skill_data_paths_ratchet`'s question of scripts, hooks,
    agents and engine config, against the shrink-only ceiling in
    `config/engine-data-paths-exemptions.json`. Not at pre-commit, for the
    reason `legacy_outputs_ratchet` gives.
    """
    proc = run_ratchet(repo, ENGINE_DATA_PATHS)
    if proc is None:
        print(f"{YELLOW}engine-data-paths ratchet skipped: "
              f"{scanner_path(repo, ENGINE_DATA_PATHS)} is absent.{RESET}")
        return
    if proc.returncode == 0:
        tail = [ln for ln in (proc.stdout + proc.stderr).splitlines()
                if "ceiling" in ln]
        for line in tail[-1:]:
            print(f"{GRAY}{line}{RESET}")
        return
    _refuse_ratchet(
        ": engine code names a data-repository address by its physical path "
        "beyond the recorded ceiling, the ceiling is above the tree, or a "
        "reviewed entry went stale.",
        proc.stdout + proc.stderr,
        "Resolve the address through `data_path()` or "
        "`python scripts/data-path.py path <name>`, or record a reviewed entry "
        "in config/engine-data-paths-exemptions.json. The ceiling may only fall.")


def runtime_write_guard_ratchet(repo: Path) -> None:
    """Refuse the push when an overlay write bypasses the front door; print the verdict on every push.

    Engine audit q32, 2026-10-16: the guard had been red on main with nothing
    running it, so its verdict line is printed on every push, pass or fail.
    """
    proc = run_ratchet(repo, RUNTIME_WRITE_GUARD)
    if proc is None:
        print(f"{YELLOW}runtime-write-guard skipped: "
              f"{scanner_path(repo, RUNTIME_WRITE_GUARD)} is absent.{RESET}")
        return
    if proc.returncode == 0:
        tail = [ln for ln in (proc.stdout + proc.stderr).splitlines()
                if ln.startswith("runtime-write-guard:")]
        for line in tail[-1:]:
            print(f"{GRAY}{line}{RESET}")
        return
    _refuse_ratchet(
        ": an overlay write does not go through the front door, or an "
        "exemption row forgives no site.",
        proc.stdout + proc.stderr,
        "Resolve the address through `data_path()`, or add a reviewed row "
        "to RECORD_EXEMPTIONS in scripts/utils/runtime_write_guard.py; delete "
        "a row that matches no site.")


def data_config_paths_ratchet(engine: Path) -> None:
    """Refuse the DATA push when its config files name data by physical path.

    HOS-234 part 5, operator decision 2026-10-13: no hard-coded data paths
    anywhere. `scripts/audit-skill-data-paths.py --data-config` scans the data
    repository's config files against the ledger at the registry name
    `data-config-exemptions`, ceiling 0. Run from the ENGINE clone, which holds
    the scanner; the scanner resolves the data root itself.
    """
    proc = run_ratchet(engine, DATA_CONFIG_PATHS)
    if proc is None:
        print(f"{YELLOW}data-config-paths ratchet skipped: "
              f"{scanner_path(engine, DATA_CONFIG_PATHS)} is absent.{RESET}")
        return
    if proc.returncode == 0:
        tail = [ln for ln in (proc.stdout + proc.stderr).splitlines()
                if "ceiling" in ln]
        for line in tail[-1:]:
            print(f"{GRAY}{line}{RESET}")
        return
    _refuse_ratchet(
        ": a data-repository config file names a data address by its physical "
        "path beyond the recorded ceiling, or a reviewed entry went stale.",
        proc.stdout + proc.stderr,
        "Name it by registry name or `group:<group>` (`python scripts/data-path.py "
        "names`), or record a reviewed entry in the ledger at the registry name "
        "`data-config-exemptions`. The ceiling may only fall.")


def engine_only_ratchet(repo: Path) -> None:
    """Refuse the push when the engine carries a tracked file that is not the engine.

    HOS-131, operator decision 2026-09-25: anything in the engine repository
    that is not the engine is a leak, invented demo content included.
    `scripts/check-engine-only.py` holds the allowlist in
    `config/engine-allowlist.yaml` against git's index, with the files that
    were outside it on the day the gate went in listed in the shrink-only
    `config/engine-only-baseline.json`. Unlike the five ratchets above it also
    runs at pre-commit, because a non-engine file in the public engine is
    failure A by the operator's own definition; this is the backstop for a
    commit made with the hooks disarmed.
    """
    proc = run_ratchet(repo, ENGINE_ONLY)
    if proc is None:
        print(f"{YELLOW}engine-only ratchet skipped: "
              f"{scanner_path(repo, ENGINE_ONLY)} is absent.{RESET}")
        return
    if proc.returncode == 0:
        tail = [ln for ln in (proc.stdout + proc.stderr).splitlines()
                if "files judged" in ln]
        for line in tail[-1:]:
            print(f"{GRAY}{line}{RESET}")
        return
    _refuse_ratchet(
        ": the engine carries a tracked file the allowlist does not cover, or "
        "the engine-only baseline went stale or grew.",
        proc.stdout + proc.stderr,
        "Move a non-engine file to the data repository; give an engine file a "
        "class or an entry with its reason in config/engine-allowlist.yaml. "
        "The baseline only shrinks.")

def engine_content_scan(repo: Path, data_root: Path, *,
                        will_commit: bool = True) -> None:
    """UNBYPASSABLE engine CONTENT-leak gate (engine only).

    The content sibling of engine_clean_scan() (routing) and content_scan()
    (secrets): refuse the push if any engine-routed file about to be pushed carries
    a real-entity token harvested from the private DATA overlay -- real person
    slugs/names, handles, e-mails, Telegram IDs, or curated company and event tokens.
    Closes the gap the 2026-06-28 public-readiness audit exposed: the structural
    guards check WHERE a file routes, never WHAT is inside it, so real data inside a
    legitimately engine-routed file slipped past every layer. Degrades to a no-op
    when the overlay is absent (public clone / CI), where the structural layers
    still hold. Suppress a true false positive inline with `content-guard: ok`.

    Reads the unpushed COMMITS as well as the disk, for the reason `content_scan`
    measures. This repository is PUBLIC, so a real name that reached any commit
    in the range is published by the push whatever the working copy says now. A
    finding from history is labelled `path@blobsha` to keep the two apart, since
    only one of them can be fixed by editing a file.
    """
    dl = build_denylist(data_root)
    if dl.degraded or not dl.tokens:
        # No overlay = a public clone or CI. Skipping is correct and quiet: the
        # structural layers still hold and there is nothing to harvest.
        if data_root is None or not Path(data_root).is_dir():
            return
        # An overlay that IS present and still produced a degraded or empty list
        # means the harvest broke. Refuse: this gate is the only layer that reads
        # WHAT is inside an engine-routed file, and a silent skip here is exactly
        # the "looks like coverage" failure the flag exists to prevent.
        print(f"{RED}REFUSING TO PUSH — the real-entity denylist could not be "
              f"built from {data_root}.{RESET}")
        print(f"{GRAY}The content gate reads no file until this is fixed. Check "
              f"config/content-denylist.yaml for a parse error, and the "
              f"contact cards, the thread homes, the Odin brain and the "
              f"auto-memory records for readability.{RESET}")
        log_denial(mechanism="push:engine-content-scan", action="push",
                   path=str(data_root), reason="denylist degraded with an overlay present")
        sys.exit(2)
    findings: list[tuple[str, int, str, str]] = []
    unscanned: list[str] = []

    # The unpushed COMMITS first, then the disk. `engine_text_rels` asks the
    # routing and suffix questions of the PATH alone, which is the only half of
    # `engine_text_files` that has an answer for a version of a file a later
    # commit deleted -- `is_file()` says no, and the push sends it anyway.
    # `published` is the set `engine_clean_scan` excuses from the routing wall;
    # passing it here is what keeps every excused blob READ.
    try:
        published = published_engine_paths(repo)
        history = [b for b in unpushed_blobs(repo)
                   if b.rel in set(engine_text_rels([b.rel], published))]
    except HistoryUnavailable as exc:
        print(f"{RED}REFUSING TO PUSH — cannot read the unpushed history of "
              f"{repo.name}: {exc}{RESET}")
        sys.exit(2)
    # A HISTORY hit is excused only when the same matched text is already in
    # origin/main's version of the SAME path: the remote publishes those bytes
    # already, so the blob adds nothing. MEASURED 2026-09-25: removing a name
    # from the denylist's own allowlist made it harvestable, and four unpushed
    # versions of the file that still carried it refused the push although
    # origin/main's copy carried it too. Nothing wider is excused: a token new
    # to the path, a path the remote lacks, and every file on disk are judged
    # exactly as before. An unreadable base version excuses nothing.
    published_tokens: dict[str, frozenset[str]] = {}
    excused = 0
    for blob in history:
        label = f"{blob.rel}@{blob.sha[:9]}"
        try:
            text = read_blob(repo, blob.sha).decode("utf-8")
        except HistoryUnavailable as exc:
            unscanned.append(f"{label}: {exc}")
            continue
        except UnicodeDecodeError as exc:
            unscanned.append(f"{label}: {exc}")
            continue
        hits = dl.scan_text(text)
        if hits and blob.rel not in published_tokens:
            base = published_bytes(repo, blob.rel)
            try:
                base_text = base.decode("utf-8") if base is not None else ""
            except UnicodeDecodeError as exc:
                print(f"{GRAY}content gate: origin/main's {blob.rel} is not "
                      f"UTF-8 ({exc}); nothing in its history is excused.{RESET}")
                base_text = ""
            published_tokens[blob.rel] = frozenset(
                matched for _l, matched, _c in dl.scan_text(base_text))
        for lineno, matched, category in hits:
            if matched in published_tokens[blob.rel]:
                excused += 1
                continue
            findings.append((label, lineno, matched, category))
    if excused:
        # Said, never silent: a narrowed sweep must not print like a full one.
        print(f"{GRAY}content gate: {excused} history hit(s) excused, each "
              f"already present in origin/main's version of the same path.{RESET}")

    gone: list[str] = []
    for rel in engine_text_files(
            repo, sorted(_push_delta_files(repo, will_commit=will_commit))):
        try:
            text = (repo / rel).read_text(encoding="utf-8")
        except FileNotFoundError:
            # THE WALK-THEN-READ RACE. `_push_delta_files` lists paths and this
            # loop reads them after, and `engine_text_files` filters on
            # `is_file()` in between, so the window is narrow but open: a file
            # created and deleted inside it made the read raise, landed in
            # `unscanned`, and REFUSED a push carrying nothing wrong. A gate
            # that blocks on its own timing is how an operator learns to reach
            # for `--no-verify`.
            #
            # WHY SKIPPING IS SAFE HERE, and the reason is NOT "the file is
            # gone" -- that argument is wrong in general, because a tracked file
            # deleted from the worktree keeps its content in the INDEX, and a
            # blind skip would let a staged secret past the last wall by
            # deleting the file after staging it.
            #
            # It is safe because of what step 3 does. MEASURED 2026-09-01: this
            # script stages with `git add -A` (see the commit step below), and
            # `git add -A` STAGES THE DELETION -- a scratch repo with a file
            # added and then removed from the worktree had an index entry
            # holding the content before `add -A` and no entry after it. So a
            # path absent from the worktree at scan time is absent from the
            # commit this run makes, and there is no content for the push to
            # carry. Narrow `git add -A` and this skip becomes a hole, silently.
            gone.append(rel)
            continue
        except (OSError, UnicodeDecodeError) as exc:
            # Recorded, then refused below. A bare `continue` here meant an
            # engine-routed file whose bytes are not valid UTF-8 -- a note saved
            # as UTF-16, a stray byte in a patch, a transient read error --
            # passed the LAST wall with no record at all, and the push was
            # reported clean over a file nobody had read. The sibling CLI
            # `content-guard.py` closed exactly this on 2026-08-14; the copy in
            # here kept the hole, so the bypassable layer was stronger than the
            # unbypassable one. Genuine binaries never reach this line:
            # `engine_text_files` drops them by suffix first.
            unscanned.append(f"{rel}: {exc}")
            continue
        for lineno, matched, category in dl.scan_text(text):
            findings.append((rel, lineno, matched, category))
    if findings:
        for rel, lineno, _matched, category in findings:
            # The matched token is a real-entity name, so it is the payload the
            # log must not carry: record where and what class, never the value.
            log_denial(mechanism="push:engine-content-scan", action="push",
                       path=f"{rel}:{lineno}", reason=f"real-entity token [{category}]")
        print(f"{RED}REFUSING TO PUSH — real-entity CONTENT in engine-routed file(s):{RESET}")
        for rel, lineno, matched, category in findings:
            print(f"  {RED}{rel}:{lineno}{RESET}  \"{matched}\"  {GRAY}[{category}]{RESET}")
        print(f"{GRAY}The engine ships no real data. Genericize to a placeholder, move the "
              f"value to the DATA overlay, or annotate the line `content-guard: ok <reason>`.{RESET}")
        if any("@" in rel for rel, _l, _m, _c in findings):
            print(f"{GRAY}A path shown as `file@blobsha` is a version carried by an "
                  f"UNPUSHED COMMIT. Editing the file now does not remove it; the "
                  f"commit range has to be rewritten before this push can go.{RESET}")
        sys.exit(2)
    if gone:
        # Not a refusal. There is no content here for this push to carry, so
        # refusing would block on the gate's own timing. It is still SAID, because
        # a corpus that shrank in silence is a narrowed check printing like a
        # complete one (`.claude/rules/scope-claims.md`).
        print(f"{GRAY}content gate: {len(gone)} path(s) listed in the delta "
              f"vanished before they could be read. `git add -A` stages that "
              f"deletion, so this push carries no content for them:{RESET}")
        for rel in gone:
            print(f"{GRAY}  {rel}{RESET}")
    if unscanned:
        for note in unscanned:
            log_denial(mechanism="push:engine-content-scan", action="push",
                       path=note.split(":", 1)[0], reason="engine-routed file could not be read")
        print(f"{RED}REFUSING TO PUSH — engine-routed file(s) the content gate "
              f"could not read:{RESET}")
        for note in unscanned:
            print(f"  {RED}{note}{RESET}")
        print(f"{GRAY}Unverified is not clean. Re-save the file as UTF-8, fix the read "
              f"error, or give it a binary suffix so the gate skips it deliberately.{RESET}")
        sys.exit(2)


# What the child process below runs, inside the engine AS COMMITTED rather than
# the engine this file was imported from. It exercises what a service on the
# host passes through before its first write: the data-address registry's
# `names:` block and its `services:` block, one runtime address resolved the way
# a writer resolves it (which is where the schema-version gate and the runtime
# store's ignore-rule gate refuse), and the routing map with the private
# overlay's rules applied. Each step names the file it reads, so a refusal can
# say WHICH file the pushed engine would not take.
#
# The routing loader fails CLOSED rather than refusing: an overlay rule file it
# cannot parse contributes nothing and every overlay path answers `private`. So
# this step catches an exception and only an exception; a rule file the engine
# silently reads as empty is not refused here, and nothing claims it is.
_ACCEPTANCE_PROBE = r"""
import json
import sys
from pathlib import Path

engine, data = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(engine))
step = "import"
where = "the engine's own modules"
try:
    from scripts.utils import data_path, runtime_dir, workspace

    # Every later step reads the data root from the environment at least in
    # part, so first prove the environment names the tip under test; otherwise
    # a step would read whatever overlay the parent sits beside and pass on
    # its behalf.
    step = "data root"
    seen = Path(workspace.get_data_root()).resolve()
    if seen != data.resolve():
        raise RuntimeError("the probe resolved the data root to " + str(seen)
                           + ", not the data tip under test")
    registry = str(data_path.registry_path(data).relative_to(data))
    step = "registry names"
    where = registry
    data_path.load_registry(data)
    step = "registry services"
    services = runtime_dir.load_services(data)
    step = "runtime address"
    where = ".schema-version, .gitignore or " + registry
    data_path.data_path(data_path.RUNTIME_SCHEME + next(iter(services)) + "/"
                        + runtime_dir.KINDS[0])
    step = "routing map"
    where = "config/routing-map.yaml (engine)"
    workspace.load_routing_map()
except BaseException as exc:
    print(json.dumps({
        "file": where, "step": step,
        "error": type(exc).__name__ + ": " + str(exc).replace(str(data), "<data>"),
    }))
    sys.exit(1)
"""

# Steps a data change cannot repair: the fault is in the engine being pushed.
_ENGINE_SIDE_STEPS = frozenset({"import", "data root"})

# A bound against a hung child, not a performance target: the probe imports a
# handful of modules and parses two small YAML files.
_ACCEPTANCE_TIMEOUT_S = 300


def _git_bytes(repo: Path, *args: str) -> bytes:
    """stdout of one git command as bytes, or HistoryUnavailable naming the failure."""
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          check=False)
    if proc.returncode != 0:
        raise HistoryUnavailable(
            f"git {' '.join(args)}: "
            f"{proc.stderr.decode('utf-8', 'replace').strip() or 'git failed'}")
    return proc.stdout


def _extract_tree(repo: Path, commit: str, paths: tuple[str, ...], dest: Path) -> None:
    """Write *paths* as *commit* holds them into *dest*, from git objects only.

    `git archive`, so nothing is read off the working tree and no worktree is
    registered in the repository's metadata. A path the commit does not hold is
    skipped rather than refused: an absent `config/` is a state the loaders
    have their own answer for, and that answer is what is being tested. The
    `data` extraction filter refuses links, devices and any member escaping
    *dest*, which matters because the archive is built from committed names.
    """
    import io
    import tarfile

    dest.mkdir(parents=True, exist_ok=True)
    present = [p for p in paths if subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{commit}:{p}"],
        capture_output=True, check=False).returncode == 0]
    if not present:
        return
    blob = _git_bytes(repo, "archive", "--format=tar", commit, "--", *present)
    with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
        tar.extractall(dest, filter="data")


def _refuse_pair(reason: str, where: str, log_reason: str) -> None:
    """Print one refusal of this wall, record it, and exit 2. Never returns."""
    print(f"{RED}REFUSING TO PUSH ENGINE — {reason}{RESET}")
    log_denial(mechanism="push:engine-accepts-data", action="push",
               path=where, reason=log_reason)
    sys.exit(2)


def engine_accepts_data(repo: Path, data_root: Path | None, data_ref: str) -> None:
    """Refuse the ENGINE push when the engine being pushed would not accept the
    data repository as origin will hold it. HOS-139.

    THE FAILURE, HOS-137, 2026-09-25. An engine commit made the registry refuse
    a `root:` key. The private registry still carried three such lines, removed
    in the working tree and never committed, and only the engine was pushed. The
    service host pulls both repositories every hour, so it took the new engine
    beside the old registry, and a bot restart-looped for about an hour on the
    refusal. Every wall above this one asks whether the engine is SAFE to
    publish; none asked whether it can RUN against the data it will meet.

    What is checked, and against what. The engine is taken at HEAD, which is
    the commit this push sends, extracted from git objects so the working
    tree's modules cannot stand in for it. The data repository is taken at
    *data_ref*, chosen in `main()` as the tip origin holds once this run's data
    step is done, and the files the engine reads from it (`config/`, the
    schema version and the ignore rules) are extracted the same way: the
    operator's working copy is exactly what HOS-137 proved origin does not
    hold. The engine's own loaders then run against that pair in a child
    process, `_ACCEPTANCE_PROBE`, which lists the steps.

    A refusal exits 2 and never touches the data push, which ran first. It is
    not `RepoNotPushable`: that is the routine skip of a repository on a feature
    branch, reported as a partial success, while this is a defect the operator
    has to act on before the engine may go. No flag skips it.

    No data repository, a public clone, is said and passes: there is no data
    for the pushed engine to meet. A data repository that is present but whose
    tip cannot be read refuses, because an unverified pair is not a compatible
    one.

    NOT CLAIMED. A push that does not run through this script, a bare
    `git push` of the engine included, is not covered. The reverse order, a
    data push that the engine already on origin would refuse, is not checked.
    `origin/main` is this clone's remote-tracking ref and is not fetched here,
    as nowhere else in this script: data pushed to origin from another clone
    would not be seen. Nothing beyond the probe's steps is exercised.
    """
    if data_root is None or not Path(data_root).is_dir():
        print(f"{GRAY}engine/data compatibility: no data repository here, so the "
              f"pushed engine meets no data; nothing to check.{RESET}")
        return
    try:
        engine_commit = _git_bytes(repo, "rev-parse", "--verify",
                                   "HEAD^{commit}").decode().strip()
        data_commit = _git_bytes(Path(data_root), "rev-parse", "--verify",
                                 f"{data_ref}^{{commit}}").decode().strip()
    except HistoryUnavailable as exc:
        _refuse_pair(f"cannot name the engine/data pair to check: {exc}",
                     str(data_root), "engine or data tip unreadable")
    pair = f"engine {engine_commit[:12]} and data {data_commit[:12]}"
    try:
        scratch_root = data_write_dir("runtime:scratch/tmp")
    except DataPathError as exc:
        _refuse_pair(f"no scratch directory to compare {pair} in: {exc}",
                     str(data_root), "no scratch directory")

    with tempfile.TemporaryDirectory(prefix="heading-push-accept-",
                                     dir=scratch_root) as scratch:
        engine_tree = Path(scratch) / "engine"
        data_tree = Path(scratch) / "data"
        try:
            _extract_tree(repo, engine_commit, ("scripts", "config"), engine_tree)
            _extract_tree(Path(data_root), data_commit,
                          ("config", ".schema-version", ".gitignore"), data_tree)
        except (HistoryUnavailable, OSError) as exc:
            _refuse_pair(f"cannot materialise {pair} to compare them: {exc}",
                         str(data_root), "pair could not be materialised")
        env = dict(os.environ, WORKSPACE_ROOT=str(engine_tree),
                   HEADING_OS_DATA=str(data_tree))
        try:
            proc = subprocess.run(
                [sys.executable, "-c", _ACCEPTANCE_PROBE, str(engine_tree),
                 str(data_tree)],
                cwd=str(engine_tree), env=env, capture_output=True, text=True,
                timeout=_ACCEPTANCE_TIMEOUT_S, check=False)
        except subprocess.TimeoutExpired:
            _refuse_pair(f"the compatibility probe over {pair} exceeded "
                         f"{_ACCEPTANCE_TIMEOUT_S}s.", str(data_root),
                         "compatibility probe timed out")

    if proc.returncode == 0:
        print(f"{GRAY}engine/data compatibility: engine {engine_commit[:12]} "
              f"accepts data {data_commit[:12]} ({data_ref}).{RESET}")
        return
    try:
        fields = json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError) as exc:
        # The probe died without reporting, before or around its own handler.
        tail = (proc.stderr.strip() or f"exit {proc.returncode}")
        fields = {"file": "unknown", "step": "unknown",
                  "error": f"{tail} (no probe report: {exc})"}
    step = fields.get("step", "unknown")
    print(f"{RED}REFUSING TO PUSH ENGINE — the engine being pushed would not "
          f"accept the data repository as origin holds it:{RESET}")
    print(f"  file:   {fields.get('file', 'unknown')}")
    print(f"  engine: {engine_commit}")
    print(f"  data:   {data_commit} ({data_ref})")
    print(f"  step:   {step}")
    print("  error:  " + str(fields.get("error", "")).replace("\n", "\n          "))
    if step in _ENGINE_SIDE_STEPS or step == "unknown":
        print(f"{GRAY}The fault is in the engine being pushed, or in this "
              f"machine's environment, not in the data: fix it there.{RESET}")
    else:
        print(f"{GRAY}If origin's data is behind this engine, push the data "
              f"change it depends on first, then the engine; anything that "
              f"pulls both, the service host's hourly pull among them, would "
              f"otherwise take this engine beside that data (HOS-137). If the "
              f"error is in the engine's own defaults, fix the engine.{RESET}")
    log_denial(mechanism="push:engine-accepts-data", action="push",
               path=str(fields.get("file", "unknown")),
               reason=f"engine {engine_commit[:12]} refuses data "
                      f"{data_commit[:12]} at step {step}")
    sys.exit(2)


ENGINE_GATE_MARKER = "run-tests.py"
DATA_GATE_MARKER = "heading-os-data-test-gate"


def _git_hooks_dir(repo: Path) -> Path | None:
    """Where git looks for `repo`'s hooks, or None when that cannot be resolved.

    `repo / ".git" / "hooks"` was read directly, and it is only right for an
    ordinary clone. In a LINKED WORKTREE (and in a submodule) `.git` is a
    gitFILE holding `gitdir: <path>`, and git resolves hooks against the COMMON
    gitdir, not the per-worktree one. MEASURED 2026-08-30 in a scratch repo with
    a worktree added: the armed hook lives at `<main>/.git/hooks/pre-push`,
    `git rev-parse --git-path hooks/pre-push` from the worktree names exactly
    that file, and the old expression pointed at a `.git/hooks` directory that
    does not exist -- so `_pre_push_gate_armed` returned False forever and every
    push from a worktree was skipped with "the pre-push test gate is not
    installed" while the gate was armed and would have run. An agent working in
    a `git worktree` is an ordinary layout here.

    Resolved in pure Python rather than by shelling out, because this feeds a
    security gate and every unresolvable shape has to fail CLOSED: an
    unreadable gitfile, a gitfile that does not start with `gitdir:`, or an
    unreadable `commondir` all return None, which the caller reads as "not
    armed". Widening it is what would be dangerous, so it never guesses.
    """
    dot_git = repo / ".git"
    if dot_git.is_dir():
        return dot_git / "hooks"
    if not dot_git.is_file():
        return None
    try:
        line = dot_git.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not line.startswith("gitdir:"):
        return None
    target = line.split(":", 1)[1].strip()
    if not target:
        # `Path("")` is `Path(".")`, which would resolve the hooks dir back onto
        # the worktree itself and answer a question this file cannot answer.
        return None
    gitdir = Path(target)
    if not gitdir.is_absolute():
        gitdir = (repo / gitdir).resolve()
    # A worktree's gitdir carries `commondir`, pointing at the shared .git that
    # actually holds `hooks/`. A submodule's gitdir has no `commondir` and holds
    # its own `hooks/`, so the absence of the file is the answer, not an error.
    commondir = gitdir / "commondir"
    if commondir.is_file():
        try:
            rel = commondir.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return None
        if not rel:
            return None
        common = Path(rel)
        gitdir = common if common.is_absolute() else (gitdir / common).resolve()
    return gitdir / "hooks"


def _pre_push_gate_armed(repo: Path, marker: str = ENGINE_GATE_MARKER) -> bool:
    """True if repo's pre-push hook is installed and runs the regression gate.

    `marker` selects which gate to look for. The engine's hook runs the engine
    suite; a DATA overlay's hook runs that overlay's own tests and then hands off
    to git-lfs. The stock git-lfs hook carries neither marker, so it correctly
    reads as "no gate".

    The pre-push hook (installed by scripts/install-git-hooks.py) is the single
    authoritative test gate -- it runs the suite on EVERY push to the engine, not
    just this path. push-all does NOT run the suite a second time itself (that was
    a redundant double-run, removed 2026-06-20); it only refuses to push when the
    hook is absent, so the gate can never be silently skipped on an un-provisioned
    clone. Mirrors install-git-hooks.pre_push_verdict (kept inline because that
    module is kebab-named and not importable).

    It deliberately does NOT mirror install-git-hooks.check_pre_push_data, which
    since 2026-08-21 also resolves the engine path stamped into the data hook.
    The two answer different questions and both answers are true: this one asks
    "will a gate run at all", and a data hook with a stale stamp still runs the
    overlay's tests (under a fallback interpreter, saying so on stderr), so
    refusing the push here would block a backup over a degraded gate rather than
    an absent one. `--check` asks "is it correctly installed" and is the surface
    that should go red. Do not collapse them into one predicate."""
    hooks = _git_hooks_dir(repo)
    if hooks is None:
        return False
    hook = hooks / "pre-push"
    try:
        return hook.is_file() and marker in hook.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # `UnicodeDecodeError` is a ValueError, so `except OSError` never caught
        # it. A pre-push hook is an arbitrary executable - a compiled binary, or
        # a shell script carrying one Latin-1 byte in a comment - and this
        # predicate is documented to fail CLOSED on every shape it cannot
        # resolve. MEASURED 2026-09-01: a hook holding `caf\xe9` raised out of a
        # function that answers True or False, past `_attempt` (which absorbs
        # only RepoNotPushable), and ended the backup on a traceback instead of
        # the named refusal. `_git_hooks_dir` above already catches the pair;
        # this is the half that was left behind.
        return False


# Set by `push_repo` in the environment of its own push, to the commit its
# step-0 walls already scanned, so the pre-push hook does not scan it twice.
# Setting it by hand to the commit being pushed skips the hook's scan exactly
# as `--no-verify` skips the hook: a deliberate bypass, never an accident.
PUSH_SCANNED_ENV = "HEADING_OS_PUSH_SCANNED"


def pre_push_scan(kind: str, repo: Path, ref_lines: str,
                  env: "dict[str, str]") -> None:
    """The push-path content walls, run from a git pre-push hook.

    `push_repo` scans before it commits, but a plain `git push` never enters it,
    so until 2026-10-16 the hook ran only the test gate and the content walls
    were skipped (engine audit A-1.1). The hook now calls this with git's ref
    lines on stdin. `kind` is `engine` or `data`, as the two hooks know which
    repository they guard: the data repository gets the secret scan, the engine
    also its routing and real-entity walls, as `push_repo` runs them.

    The walls scan `origin/main..HEAD`, so a ref whose commit is not HEAD is
    refused rather than pushed unscanned. A ref deletion carries no content.
    Every refusal exits non-zero, which makes git refuse the push.
    """
    shas: set[str] = set()
    for line in ref_lines.splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 4:
            print(f"{RED}REFUSING TO PUSH — unreadable pre-push ref line: "
                  f"{line!r}{RESET}")
            sys.exit(2)
        if set(parts[1]) != {"0"}:
            shas.add(parts[1])
    if not shas:
        return
    if shas == {env.get(PUSH_SCANNED_ENV, "")}:
        print(f"{GRAY}pre-push content scan: {shas.pop()[:12]} was scanned "
              f"by push-all before its commit{RESET}")
        return
    head = run(["git", "rev-parse", "HEAD"], repo, check=False).stdout.strip()
    if shas != {head}:
        print(f"{RED}REFUSING TO PUSH — the pre-push content scan covers the "
              f"checked-out branch only, and this push sends another commit. "
              f"Check it out and push it, or use scripts/push-all.py.{RESET}")
        sys.exit(2)
    content_scan(repo, will_commit=False)
    if kind == "engine":
        data_root = Path(get_data_root())
        if data_root.resolve() == repo.resolve():
            return
        engine_clean_scan(repo)
        engine_content_scan(repo, data_root, will_commit=False)


def run(args, cwd, check=True):
    """Run a git command, returning CompletedProcess. Raises on non-zero when check."""
    return subprocess.run(args, cwd=str(cwd), check=check,
                          capture_output=True, text=True)


def gh_token() -> str | None:
    """Return GH_TOKEN (the variable gh reads), loading the engine .env if needed."""
    if "GH_TOKEN" not in os.environ:
        try:
            load_env(get_workspace_root())  # loads engine .env into os.environ
        except Exception as exc:
            logger.warning("push-all: failed to load engine .env: %s", exc)
    return os.environ.get("GH_TOKEN") or None


def refresh_lific_archive(repo: Path) -> None:
    """Write a fresh `lific dump` into the overlay's one tracked archive.

    Never aborts the push. The reasoning is a comparison, not a preference: a
    failed dump leaves the PREVIOUS archive in place, so the operator keeps an
    older copy of the tracker, while refusing the push would leave every other
    record on this machine alone. The failure is printed at full width instead,
    because a stale backup that nobody was told about is the one outcome worse
    than no backup at all.

    `lific` absent exits 0 and says so in grey: a machine without the tracker has
    nothing to dump, and that is not a fault.
    """
    script = Path(__file__).resolve().parent / "lific-backup.py"
    if not script.is_file():  # a clone that predates it
        return
    proc = subprocess.run([sys.executable, str(script), "--quiet"],
                          cwd=str(repo), capture_output=True, text=True,
                          check=False)
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode == 0:
        if out:
            print(f"  {out}")
        return
    print(f"{RED}{BOLD}the Lific archive was NOT refreshed (exit "
          f"{proc.returncode}); this push carries the previous one, or none"
          f"{RESET}")
    for line in (err or out or "no output").splitlines():
        print(f"  {line}")


def push_repo(name: str, repo: Path, message: str, do_commit: bool, dry_run: bool,
              push_env: dict, is_engine: bool = False, data_root: Path | None = None,
              test_gate: bool = False,
              gate_marker: str = ENGINE_GATE_MARKER,
              data_ref: str = "origin/main") -> None:
    """Commit + push one repo to origin/main, then verify ahead/behind == 0 0.

    *data_ref* is read only for the engine: the data repository's ref holding
    the tip origin will have once this run's data step is done.
    """
    print(f"\n{BOLD}{CYAN}== {name}: {repo} =={RESET}")

    # 0-. refresh the one tracked Lific archive, DATA only, before anything reads
    # the tree. Every path Lific writes is ignored by the overlay (its database,
    # the two sidecars, and the hourly `backups/` it rotates), so without this the
    # issue tracker has no off-machine copy at all. Placed ABOVE the content scan
    # on purpose: a file written after the scan is a file the scan never saw, and
    # `git add -A` three steps down would commit it anyway. That one-step-behind
    # gap is the same one steps 0a and 1 below were moved to close.
    if not is_engine and do_commit and not dry_run:
        refresh_lific_archive(repo)

    # 0. engine/data leak gate (engine only): the engine clone must be code-only.
    # Unbypassable -- runs before the commit, so even a working tree staged with
    # --no-verify cannot push a data-class artifact out of the engine. The DATA
    # repo legitimately carries private files and is exempt.
    # 0a. secret CONTENT scan, for every repo, BEFORE the commit.
    #
    # It used to run at step 3.5, after this function's own `git add -A && git
    # commit`. Nothing left the machine either way - the push was still refused -
    # but the secret was in local history by then, so the repair was a history
    # scrub instead of an edit. The two walls beside it have always run here, and
    # `_push_delta_files`' own docstring says step 0 is deliberate "so that a tree
    # staged with --no-verify cannot slip past". `scripts/publish-service.py`
    # already scans before its `git add -A` for the same reason.
    #
    # No coverage is lost by moving it: `_push_delta_files` includes untracked
    # files, so the set here is the same one the commit is about to create.
    content_scan(repo, will_commit=do_commit)

    if not is_engine:
        data_config_paths_ratchet(get_workspace_root())

    if is_engine:
        engine_clean_scan(repo)
        if data_root is not None:
            engine_content_scan(repo, data_root, will_commit=do_commit)
        data_path_ratchet(repo)
        legacy_outputs_ratchet(repo)
        import_env_purity_ratchet(repo)
        outside_workspace_writes_ratchet(repo)
        engine_dir_writes_ratchet(repo)
        engine_only_ratchet(repo)
        skill_data_paths_ratchet(repo)
        engine_data_paths_ratchet(repo)
        runtime_write_guard_ratchet(repo)

    # 1. pre-push secret scan over every file this push would CARRY
    #
    # Tracked AND untracked-not-ignored, not `git ls-files` alone. Step 3 below
    # runs `git add -A`, which makes untracked files tracked, and this wall never
    # ran again - so a credential this very run was about to commit was never
    # tested by it. The gap had no backstop either: `.gitignore` carries
    # `.sessions/` and one exact browser `cookies.json` under `outputs/`, not a bare
    # `*.session` or `cookies.json` rule, and `scripts/secret-scanner.py` lists
    # `.session` in SKIP_EXTENSIONS - so the content scan returns clean for
    # precisely this file type. A `telegram.session` dropped at the repo root
    # walked through all three layers.
    #
    # `repo_carried_paths` is the shared resolver the routing wall at step 0
    # already uses, so the two walls now agree about what "about to be pushed"
    # means. It runs `-z` for both lists, which is what the paragraph below is
    # about.
    #
    # `-z`, for the reason `_push_delta_files` records. Without it git C-quotes any path holding a
    # non-ASCII byte and wraps it in double quotes, and the DATA clone carries
    # such paths today. The trailing quote defeats every `$`-anchored branch of
    # SECRET_TRACKED, so a tracked `.env`, `*.session` or `cookies.json` under a
    # Cyrillic-named directory was not refused; the leading quote defeats the
    # `.memory-index/` prefix test in step 2 outright. content_scan() is no
    # backstop -- it scans the push DELTA, and this step exists precisely for a
    # credential tracked long before the push.
    carried = repo_carried_paths(repo)
    leaks = [
        f for f in carried
        if SECRET_TRACKED.search(f) and not f.endswith((".example", ".sample", ".template"))
    ]
    if leaks:
        # Counted like the two walls above it, and it is the only refusal in this
        # file that needed adding: the three below are configuration and
        # precondition failures, which the counter deliberately leaves out, while
        # this one is a leak guard proper. content_scan() does not cover it —
        # that scans the push DELTA (_push_delta_files), so a credential file
        # tracked since long before this push is refused here and would otherwise
        # be refused with nothing recording that it happened.
        for rel in leaks:
            log_denial(mechanism="push:secret-tracked-files", action="push",
                       path=rel, reason="secret-like path tracked in the repository")
        print(f"{RED}REFUSING TO PUSH — secret-like tracked files:{RESET}")
        for f in leaks:
            print(f"  {RED}{f}{RESET}")
        print(f"{GRAY}Remove from the index (git rm --cached) and add to .gitignore.{RESET}")
        sys.exit(2)

    # 2. assert the rebuildable index is not going to be pushed
    #
    # `carried`, the same set as step 1 above. This read `git ls-files` alone,
    # so an UNTRACKED and not-ignored `.memory-index/` passed here and was made
    # tracked by `git add -A` three lines down - the same one-step-behind gap the
    # filename wall had.
    if any(f.startswith(".memory-index/") for f in carried):
        print(f"{RED}REFUSING TO PUSH — .memory-index/ would be pushed "
              f"(it is rebuildable and must be gitignored).{RESET}")
        sys.exit(2)

    # 3. commit staged changes
    status = run(["git", "status", "--short"], repo).stdout.strip()
    if status and do_commit:
        if dry_run:
            print(f"{YELLOW}[dry-run]{RESET} would commit:\n{status}")
        else:
            try:
                run(["git", "add", "-A"], repo)
                run(["git", "commit", "-m", message], repo)
            except subprocess.CalledProcessError as exc:
                # The commonest failure here is a pre-commit hook refusing the
                # commit, and `run` captures output -- so without this the
                # operator saw a bare traceback and the scanner's explanation of
                # WHAT it refused was swallowed with the CompletedProcess.
                sys.stdout.write(exc.stdout or "")
                sys.stderr.write(exc.stderr or "")
                print(f"{RED}COMMIT REFUSED in {repo.name} "
                      f"(exit {exc.returncode}) — see the hook output above.{RESET}")
                sys.exit(2)
            head = run(["git", "rev-parse", "--short", "HEAD"], repo).stdout.strip()
            print(f"{GREEN}committed{RESET} {head}: {message.splitlines()[0]}")
    elif status and not do_commit:
        print(f"{YELLOW}uncommitted changes left (--no-commit):{RESET}\n{status}")
    else:
        print(f"{GRAY}no local changes to commit{RESET}")


    # 4. per-repository push preconditions.
    #
    # Three refusals sit here, AFTER the commit above, and that position is the
    # decision rather than an accident: a repo that cannot be pushed still gets
    # its local commit, so work in progress is never lost to a refusal about
    # where it can go. Raising before step 3 would have been tidier and would
    # have thrown the work away.
    #
    # They also sit ABOVE the dry-run return, which they did not before. The
    # branch check used to be below it, so a dry run reported no skip at all:
    # it hid the one thing this whole change exists to surface. Evaluating a
    # precondition writes nothing, so a dry run can afford to be honest.
    # 4a. remote identity, and it runs FIRST inside this block on purpose.
    #
    # The two refusals below raise RepoNotPushable, which _attempt absorbs, so a
    # repository on a feature branch never reaches any check placed after them.
    # A misconfigured remote must not be maskable by a routine skip, so it is
    # evaluated before either of them and stops the run outright.
    #
    # sys.exit(2) rather than RepoNotPushable, and the classification is the
    # decision: a branch that is not main says THIS repository cannot be pushed
    # and nothing about the others. A remote pointing somewhere it must not
    # says the configuration is wrong, which makes every repository in the run
    # suspect for the same reason.
    # `env=push_env`: the precondition must ask git the same question, in the
    # same environment, that the push at the chokepoint will. Without it this
    # check reads the ambient world while the push runs `push_env`.
    objection = remote_objection(repo, token=push_env.get("GH_TOKEN"),
                                 env=push_env)
    if objection:
        print(f"{RED}REFUSING TO PUSH — {objection}{RESET}")
        # Both sides of the comparison, not just the pushing repository's remote:
        # an operator who wired a convenience remote for the data repo onto the
        # engine clone would otherwise see only a URL that is correct on its own
        # and no way to see what it collided with.
        print(f"{GRAY}Check this repo's remote with: "
              f"git -C {repo} remote get-url --push origin{RESET}")
        print(f"{GRAY}Check the engine's remotes with: "
              f"git -C {get_workspace_root()} remote -v{RESET}")
        sys.exit(2)

    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo).stdout.strip()
    if branch != "main":
        raise RepoNotPushable(
            f"branch is '{branch}', expected 'main'. Merge it and push from main."
        )
    # Keyed on test_gate, NOT on is_engine, and the difference is load-bearing.
    # The single authoritative test gate is the engine's pre-push hook, which runs
    # the suite on EVERY push to the engine and not just on this path, so an
    # un-provisioned clone must not be able to push past it. A fresh clone starts
    # unarmed, because git does not share .git/hooks.
    #
    # It moved here from main() in the same change that introduced
    # RepoNotPushable: it is a precondition of pushing TO THE ENGINE REMOTE, never
    # of running the program, and while it lived in main() an unarmed hook
    # cancelled the DATA backup too. But main() checked it ABOVE the single-repo
    # branch, so it covered the pre-cutover mode as well -- and that mode pushes
    # this same engine clone with is_engine deliberately OFF, because its data
    # files are tracked legitimately there and engine_clean_scan would flag all of
    # them. Keying this raise on is_engine would therefore have narrowed a check
    # from two modes to one while looking like a pure move. Hence two flags.
    if test_gate and not _pre_push_gate_armed(repo, marker=gate_marker):
        which = "data overlay" if gate_marker == DATA_GATE_MARKER else "engine"
        raise RepoNotPushable(
            f"the {which} pre-push test gate is not installed, so a push would skip "
            "the suite. Arm it once with: python scripts/install-git-hooks.py"
        )

    # 4b. Already in sync: there is nothing to push, so do not push.
    #
    # MEASURED 2026-09-17, twice, inside the HEADING OS 2 migration's step 02.
    # The engine was `[0 0]` with a clean worktree, so this call had nothing to
    # send -- and it still cost 19:30 and then 20:22, and failed both times.
    # `git push` ran the pre-push hook anyway and handed it NO ref lines, which
    # the gate reads as "the range this push adds is unknown" and answers with
    # the whole suite (27951 tests). Under that one invocation 69 and then 68 of
    # them failed, a different set each time; the same tests are green run
    # alone, green as a group, and green as a full `run-tests.py` pass on the
    # same tree minutes later. So the backup was blocked by a suite that only
    # goes red inside a push that sends nothing.
    #
    # The question asked here is the IDENTICAL one `supervised_push` asks
    # afterwards as its postcondition (`ahead_behind(...) == (0, 0)`, git_push.py
    # line 763), down to the environment, which is why this is a precondition
    # and not a second opinion: a state the push would have to ESTABLISH already
    # holds. `None` means git could not answer, and that is not the same as
    # being in sync, so it falls through and pushes.
    #
    # Deliberately BELOW every wall above. The scans, the remote-identity
    # refusal, the branch check and the armed-gate check all still run on a
    # repository that turns out to have nothing to send: this skips the push,
    # never a check. And it sits ABOVE the dry-run return so a dry run reports
    # the skip rather than promising a push that would not happen.
    if ahead_behind(repo, env=push_env) == (0, 0):
        print(f"{GRAY}already [0 0] in sync with origin/main; nothing to push{RESET}")
        return

    # 4c. the engine being pushed must accept the data origin will hold (HOS-139).
    # Here and not at step 0, because the engine commit it judges is HEAD, and
    # HEAD is what this push sends only once step 3 has committed. Below the
    # in-sync return, because an engine with nothing to send changes nothing on
    # origin; above the dry-run return, so a dry run reports the refusal too.
    if is_engine:
        if dry_run and do_commit and status:
            print(f"{YELLOW}[dry-run]{RESET} the compatibility check judges "
                  f"the engine's HEAD; the uncommitted changes above are not "
                  f"part of it.")
        engine_accepts_data(repo, data_root, data_ref)

    if dry_run:
        print(f"{YELLOW}[dry-run]{RESET} would push origin main")
        return

    # 5. supervised push + verify ahead/behind == 0 0 in one primitive.
    # The watchdog bounds the push by inactivity (no output AND no CPU), never by
    # a wall-clock guess, so the engine's pre-push test gate is never clipped; the
    # ahead/behind == 0 0 postcondition is checked without an unbounded fetch on
    # the critical path (a bare push can silently leave the ref behind).
    # The data repository's `runtime:push/logs` rather than the system temp
    # directory, and the reason is the other half of the same defect as the
    # printed path below. `/tmp` on this machine is emptied at boot, so a path
    # reported at 15:57 can name nothing at all the next morning: a refusal whose
    # evidence expires overnight is only half-visible, and the half that
    # survives is the half nobody needs. A log carrying the operator's overlay
    # paths belongs in his private repository; it sat in the engine checkout's
    # gitignored `.logs/push/` until 2026-09-25 (HOS-5 section 7).
    #
    # No data repository means no push log, and that refuses the push rather
    # than pushing unlogged: pushes happen in HELM, where the overlay exists.
    #
    # NOT PRUNED, deliberately. `supervise.run_supervised` refuses to delete a
    # log it was given a directory for, and this adds one file of roughly 40 KB
    # per push ATTEMPT. Pushes here are manual and happen in HELM alone, so the
    # growth is slow; an auto-expiry would be the workspace's own prohibition on
    # retiring records without the operator's word, spent on a convenience.
    try:
        push_logs = data_write_dir("runtime:push/logs")
    except DataPathError as exc:
        print(f"{RED}REFUSING TO PUSH — no place for the push log: {exc}{RESET}")
        sys.exit(2)
    head = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    v = supervised_push(repo, env={**push_env, PUSH_SCANNED_ENV: head},
                        stall_window=180, label=f"push:{name}",
                        log_dir=str(push_logs))
    if v["state"] == "ok":
        print(f"{GREEN}pushed & verified [0 0] in sync with origin/main "
              f"{GRAY}({v['elapsed_s']}s){RESET}")
    else:
        print(f"{RED}{v['state'].upper()} after push — {v['reason']}{RESET}")
        if v.get("tail"):
            print(f"{GRAY}{v['tail']}{RESET}")
        # The tail is the last 25 lines and 16 KB of the run, which is the right
        # amount when a TEST fails: pytest prints its summary last, so the cause
        # is inside the window. It is the wrong amount when a session-level guard
        # fails the run, because those write before the summary and the window
        # holds warnings instead. MEASURED 2026-09-20: a push refused at
        # `28550 passed, 6 skipped, 0 failed` and `pytest exit 1`, and the reason,
        # the overlay-reachability guard in tests/conftest.py naming its own
        # counts, sat above the cut with only pydantic deprecations visible. The
        # whole log was on disk the entire time and `supervise.run_supervised`
        # returns its path saying, in its own comment, that it does so "precisely
        # so a human can open it AFTER the run". This branch never printed it.
        #
        # So the path is printed BESIDE the tail rather than instead of it:
        # dropping the tail to show a path would trade one blindness for another.
        if v.get("log_path"):
            print(f"{GRAY}full log: {v['log_path']}{RESET}")
        sys.exit(1)


def _attempt(skipped: list[tuple[str, str]], name: str, *args, **kwargs) -> None:
    """Push one repo. Record a per-repository refusal; let everything else fly.

    ONLY RepoNotPushable is absorbed. A sys.exit from any security refusal (the
    content scan, the engine clean scan, the engine content scan, a secret-like
    tracked filename) raises SystemExit, which is NOT an Exception subclass and is
    therefore not caught here even by accident. That is the invariant this helper
    exists to hold: unbypassable walls stay unbypassable.
    """
    try:
        push_repo(name, *args, **kwargs)
    except RepoNotPushable as exc:
        print(f"{YELLOW}SKIPPED {name}{RESET} {exc}")
        skipped.append((name, str(exc)))


def _report_skips(skipped: list[tuple[str, str]], args, attempted: int) -> None:
    """Print the closing summary for a partial run and exit 3. Never returns.

    One place for this text, so the reassurance below cannot drift out of step
    with the flags it is a claim about. (A second call path, for an executive
    workspace, was removed with the executive model on 2026-09-25.)

    *attempted* is how many repositories this mode tries to push, and the headline
    branches on it because exit 3 has two shapes, not one. The pre-cutover mode
    pushes exactly ONE repository, so a skip there is a backup that pushed
    NOTHING -- and the word "partial" over a run that pushed nothing is a false
    success claim about the only irreplaceable half of the workspace, which is the
    harm this command exists to prevent. That mode can produce no other shape.
    """
    all_skipped = len(skipped) == attempted
    if all_skipped:
        print(f"\n{RED}{BOLD}NOTHING PUSHED: all {attempted} repo(s) skipped.{RESET}")
    else:
        print(f"\n{YELLOW}{BOLD}Partial: {len(skipped)} of {attempted} repo(s) "
              f"not pushed.{RESET}")
    for name, reason in skipped:
        print(f"  {YELLOW}{name}{RESET}  {reason}")
    # "Everything that could be pushed was" is dropped when nothing was pushed: it
    # is true but reads as reassurance, and there is nothing to be reassured about.
    went = "" if all_skipped else "Everything that could be pushed was. "
    # The reassurance is CONDITIONAL, because it is a claim about the disk and it
    # is false in two of the three modes. Under --dry-run nothing was committed
    # (step 3 only printed what it would do), and under --no-commit nothing was
    # committed either. A dry run is also the newly reachable skip path -- the
    # branch check moved above the dry-run return so that it reports -- so an
    # unconditional "committed locally" would be wrong precisely where this
    # change added the message.
    if args.dry_run:
        print(f"{GRAY}Nothing was written: this was a dry run.{RESET}")
    elif args.no_commit:
        print(f"{GRAY}{went}--no-commit was passed, so any working-tree changes "
              f"above are still uncommitted.{RESET}")
    else:
        print(f"{GRAY}{went}Nothing was lost: each repo above is committed "
              f"locally.{RESET}")
    sys.exit(3)


def main() -> None:
    # The pre-push hooks' entry: read-only, so it runs before the HELM check,
    # in whichever repository git is pushing.
    if len(sys.argv) >= 2 and sys.argv[1] == "--pre-push-scan":
        hp = argparse.ArgumentParser(prog="push-all.py --pre-push-scan")
        hp.add_argument("--pre-push-scan", dest="kind", required=True,
                        choices=("engine", "data"))
        hp.add_argument("--repo", type=Path, default=Path.cwd())
        hargs = hp.parse_args()
        pre_push_scan(hargs.kind, hargs.repo.resolve(), sys.stdin.read(),
                      dict(os.environ))
        return
    require_main_clone(__file__)
    ap = argparse.ArgumentParser(description="Push both HEADING OS repos to their private remotes.")
    ap.add_argument("-m", "--message", help="commit message (default: dated backup message)")
    ap.add_argument("--no-commit", action="store_true", help="push existing commits only; do not commit working-tree changes")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen; make no commits or pushes")
    args = ap.parse_args()

    message = args.message or f"chore: workspace backup {datetime.now(get_default_tz()).strftime('%Y-%m-%d %H:%M')}"

    token = gh_token()
    if not token and not args.dry_run:
        print(f"{RED}GH_TOKEN not found in engine .env — cannot authenticate push.{RESET}")
        sys.exit(2)
    push_env = dict(os.environ)
    if token:
        push_env["GH_TOKEN"] = token

    engine = get_workspace_root()

    data = get_data_root()
    skipped: list[tuple[str, str]] = []
    attempted = 1 if data == engine else 2
    if data == engine:
        # Pre-cutover single repo: data files are legitimately tracked here, so the
        # engine-clean gate would flag everything. Do not arm it in this mode.
        #
        # test_gate=True even so, and that is the point of the flag being separate
        # from is_engine. This mode pushes the ENGINE clone, to the engine remote,
        # whose pre-push hook is the single authoritative suite gate. The check used
        # to sit above this branch in main() and so covered this call; keying it on
        # is_engine would have dropped it here while looking like a pure move.
        print(f"{YELLOW}Data root == engine root (pre-cutover/single repo). Pushing one repo.{RESET}")
        _attempt(skipped, "repo", engine, message, not args.no_commit, args.dry_run,
                 push_env, test_gate=True)
    else:
        # DATA FIRST, and the reason is measured rather than aesthetic: the
        # engine's pre-push hook runs the full regression suite inside the push
        # and took 320 seconds on the machine this was written on. The data
        # overlay is the only half that cannot be reconstructed, so it does not
        # queue behind a several-minute gate that may fail, stall, or be
        # interrupted.
        #
        # This costs nothing in safety, which is worth saying because the phrase
        # "stop the world" invites the opposite conclusion. Each scan protects the
        # repository it runs on: the engine scans look for a problem in the
        # ENGINE's files, while the DATA overlay runs its own content_scan over
        # its own files and legitimately carries private content. No engine
        # refusal carries any information about whether DATA is safe to push.
        _attempt(skipped, "DATA", data, message, not args.no_commit, args.dry_run, push_env,
                 test_gate=True, gate_marker=DATA_GATE_MARKER)
        # The data tip the engine is judged against (HOS-139). After a real data
        # step, pushed or already in sync, `origin/main` IS that tip, since the
        # push verified [0 0]; after a skipped one it is what origin still
        # holds. A dry run pushed nothing, so where data was not skipped the tip
        # it WOULD push is the local HEAD.
        data_skipped = any(name == "DATA" for name, _reason in skipped)
        data_ref = "HEAD" if args.dry_run and not data_skipped else "origin/main"
        if data_ref == "HEAD" and not args.no_commit and run(
                ["git", "status", "--porcelain"], data).stdout.strip():
            # A real run would commit these and push them before the engine is
            # judged, so a dry run cannot see the tip that run would produce.
            print(f"{YELLOW}[dry-run]{RESET} the data repository has uncommitted "
                  f"changes; the engine/data compatibility check below judges "
                  f"its HEAD without them, so its verdict may differ from a "
                  f"real run's.")
        _attempt(skipped, "ENGINE", engine, message, not args.no_commit, args.dry_run,
                 push_env, is_engine=True, data_root=data, test_gate=True,
                 data_ref=data_ref)

    if skipped:
        _report_skips(skipped, args, attempted)

    # Branched on `attempted`, because "Both repos pushed." over the pre-cutover
    # mode -- which announces "Pushing one repo." nine lines up -- is the closing
    # headline claiming a second off-machine copy that does not exist. Every
    # other summary line in this file is careful about exactly that, and this one
    # was not until 2026-08-30.
    headline = "Both repos pushed." if attempted > 1 else "Repo pushed."
    print(f"\n{GREEN}{BOLD}{headline}{RESET}" if not args.dry_run else f"\n{YELLOW}dry-run complete.{RESET}")


if __name__ == "__main__":
    main()
