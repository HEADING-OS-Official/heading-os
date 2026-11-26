#!/usr/bin/env python3
"""Create or remove a YARD, under the Herdr TUI or under herdr-core.

Usage:
    .venv/bin/python scripts/yard.py create <branch> [--dry-run]
    .venv/bin/python scripts/yard.py remove <branch> [--dry-run]
    .venv/bin/python scripts/yard.py remove <code> [--dry-run]     # herdr-core: w100

The backend is read from the caller's environment and from nothing else:
`HERDR_PANE_ID` set means the TUI (`herdr` 0.9.5), otherwise `HC_TERMINAL_ID`
set means herdr-core (`hc`). Neither is an error, never a guess.

Under the TUI, `create` is exactly `herdr worktree create`, whose plugin event
runs `yard-bootstrap.sh`, and `remove` is `herdr worktree remove --workspace
<id>`, whose event runs `yard-teardown.sh`. herdr-core has no worktree command
and no plugin event, so under core this script does what the plugin did: `git
worktree add` into the same yard directory the TUI uses, then the bootstrap with
a hand-made event (it starts the agent in an `hc` terminal named after the
branch); on `remove`, `hc kill` and `hc rm` of that terminal, `git worktree
remove`, then the teardown.

The yard directory is `<engine clone's parent>/.yard/<engine clone's name>/
<branch>`, derived from `main_clone_path()`, never typed.

Under herdr-core the argument of `create` is a slug, and the branch is
`w<N>-<slug>`: N is allocated by `scripts/utils/yard_code.py` (first code
w100, never reused), so the branch, the folder, the hc terminal and the space
the window names after the folder all carry the code. A slug already shaped
`w<N>-...` is refused. `remove` under core also takes the bare code (`w100`),
resolved to its one registered worktree by `yard_for_code`; zero or several
matches refuse with exit 2. The agent's terminal carries fresh `herdr.space`
and `herdr.tab` labels, which make the window show the yard as its own space
(HOS-379). The TUI path takes the branch as before and refuses a bare code.

Exit codes: 0 done; 2 bad arguments, no backend or bad branch name; 3 `remove`
of a branch not merged into `main`; 4 `create` when the branch or the yard
directory already exists, or `remove` when the yard directory or its TUI
workspace cannot be found, or (under core) when the yard has uncommitted or
untracked changes, checked before the first `hc` call; 5 a step failed partway
(nothing is rolled back; the steps that completed and the commands that would
finish the job are printed).

`--dry-run` prints every command a real run would execute, in order, and runs
nothing but the read-only `git rev-parse`, `git worktree list`, `git
merge-base --is-ancestor` and, under core, `git status --porcelain` in the
yard. It is answered before `require_main_clone`, so it
works from a yard; a real run is HELM's only.

`remove` never passes `--force` and never deletes the branch.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.utils.clone_guard import (  # noqa: E402
    CloneGuardError,
    main_clone_path,
    require_main_clone,
)
from scripts.utils.yard_code import (  # noqa: E402
    CODED_BRANCH_RE,
    YardCodeError,
    is_code,
    next_code_number,
    yard_for_code,
)

PLUGIN_DIR = Path("scripts") / "herdr" / "heading-os-yard"

EXIT_OK, EXIT_ARGS, EXIT_UNMERGED, EXIT_EXISTS, EXIT_PARTIAL = 0, 2, 3, 4, 5

#: hc's terminal-name rule, as `hc run --name` states it: 1-32 of a-z 0-9 - _ .
#: starting with a letter or digit. The coded branch is the terminal name.
HC_TERMINAL_NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}")

#: `yard-bootstrap.sh` exits 3 when the contour is provisioned (status `ok` at
#: step 11) but the agent's launch failed; 1 is a broken contour.
BOOTSTRAP_AGENT_NOT_STARTED = 3
#: The bootstrap's defaults, for the printed recovery line only.
DEFAULT_AGENT_CMD = "claude --dangerously-skip-permissions"
FIRST_PROMPT = "Reply with the single word READY, then wait for the brief from HELM."

#: `hc` answers exit 3 with `error.kind == "not_found"` for a terminal it does
#: not know (`hc --skill`, "Exit codes include 3 not_found").
HC_NOT_FOUND = 3
#: ... and exit 11 with `error.kind == "finished"` (same source, "11 finished"),
#: read here as `hc kill` of a terminal whose program already ended, which is
#: the state `remove` wants. Taken from `hc --skill`, NOT measured on a live
#: `hc kill`.
HC_FINISHED = 11


@dataclass
class Step:
    argv: list[str]
    cwd: Path
    env: dict[str, str] = field(default_factory=dict)
    #: Exit codes that count as done, beyond 0.
    tolerated: tuple[int, ...] = ()

    def spelled(self) -> str:
        prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in self.env.items())
        line = shlex.join(self.argv)
        return f"(cd {shlex.quote(str(self.cwd))} && {prefix + ' ' if prefix else ''}{line})"


def backend(environ: dict[str, str]) -> str | None:
    if environ.get("HERDR_PANE_ID"):
        return "tui"
    if environ.get("HC_TERMINAL_ID"):
        return "core"
    return None


def branch_refusal(branch: str) -> str | None:
    if not branch:
        return "the branch name is empty"
    if branch.startswith("-"):
        return f"the branch name {branch!r} starts with '-'"
    if "/" in branch:
        return f"the branch name {branch!r} contains '/'"
    return None


def yard_dir(engine: Path, branch: str) -> Path:
    return engine.parent / ".yard" / engine.name / branch


def event_json(yard: Path, branch: str) -> str:
    return json.dumps({"data": {"worktree": {"path": str(yard), "branch": branch}}})


def git_ok(engine: Path, *args: str) -> bool:
    """One read-only git question in the engine clone, answered by its exit."""
    return subprocess.run(["git", "-C", str(engine), *args], capture_output=True,
                          text=True, check=False, timeout=60).returncode == 0


def registered_worktrees(engine: Path) -> list[Path]:
    completed = subprocess.run(["git", "-C", str(engine), "worktree", "list", "--porcelain"],
                               capture_output=True, text=True, check=False, timeout=60)
    if completed.returncode != 0:
        return []
    return [Path(line.split(" ", 1)[1]).resolve() for line in completed.stdout.splitlines()
            if line.startswith("worktree ")]


def yard_dirt(yard: Path) -> str:
    """"" when `git status --porcelain` in `yard` prints nothing, else why not."""
    completed = subprocess.run(["git", "-C", str(yard), "status", "--porcelain"],
                               capture_output=True, text=True, errors="replace",
                               check=False, timeout=60)
    if completed.returncode != 0:
        return f"git status failed: {completed.stderr.strip()[:200]}"
    changed = [line for line in completed.stdout.splitlines() if line.strip()]
    return f"{len(changed)} path(s)" if changed else ""


def tui_workspace_for(yard: Path) -> str | None:
    """`workspace_id` of the TUI workspace whose checkout is `yard`, or None."""
    completed = subprocess.run(["herdr", "workspace", "list"], capture_output=True,
                               text=True, check=False, timeout=60)
    if completed.returncode != 0:
        print(f"yard.py: herdr workspace list failed: {completed.stderr.strip()[:300]}",
              file=sys.stderr)
        return None
    try:
        workspaces = json.loads(completed.stdout)["result"]["workspaces"]
    except (ValueError, KeyError, TypeError) as exc:
        print(f"yard.py: herdr workspace list was unreadable ({type(exc).__name__})",
              file=sys.stderr)
        return None
    for workspace in workspaces if isinstance(workspaces, list) else []:
        if not isinstance(workspace, dict):
            continue
        worktree = workspace.get("worktree")
        checkout = worktree.get("checkout_path") if isinstance(worktree, dict) else None
        if checkout and Path(checkout).resolve() == yard.resolve() and workspace.get("workspace_id"):
            return str(workspace["workspace_id"])
    return None


def node_path_export() -> str:
    """` PATH=<literal>` for the agent's opening export, or "" when unsafe.

    The same rule as the herdr-core branch of yard-bootstrap.sh, which says
    why: `bash -c` reads no `.bashrc`, so without this a yard has no nvm node.
    The value is this process's PATH, used only when node resolves on it,
    every entry is absolute and it is plain path text.
    """
    path = os.environ.get("PATH", "")
    if not shutil.which("node") or not re.fullmatch(r"/[A-Za-z0-9_./+@-]*(:/[A-Za-z0-9_./+@-]*)*", path):
        return ""
    return f" PATH={path}"


def create_steps(kind: str, engine: Path, branch: str) -> list[Step]:
    yard = yard_dir(engine, branch)
    if kind == "tui":
        return [Step(["herdr", "worktree", "create", "--cwd", str(engine),
                      "--branch", branch, "--base", "main"], engine)]
    return [
        Step(["git", "worktree", "add", "-b", branch, str(yard), "main"], engine),
        Step(["bash", str(PLUGIN_DIR / "yard-bootstrap.sh")], yard,
             {"HERDR_PLUGIN_EVENT_JSON": event_json(yard, branch),
              "HEADING_OS_YARD_BACKEND": "core"}),
    ]


def remove_steps(kind: str, engine: Path, branch: str, workspace: str,
                 yard: Path) -> list[Step]:
    if kind == "tui":
        return [Step(["herdr", "worktree", "remove", "--workspace", workspace], engine)]
    return [
        Step(["hc", "kill", branch], engine, tolerated=(HC_NOT_FOUND, HC_FINISHED)),
        Step(["hc", "rm", branch], engine, tolerated=(HC_NOT_FOUND,)),
        Step(["git", "worktree", "remove", str(yard)], engine),
        Step(["bash", str(engine / PLUGIN_DIR / "yard-teardown.sh")], engine,
             {"HERDR_PLUGIN_EVENT_JSON": event_json(yard, branch)}),
    ]


def run_steps(steps: list[Step]) -> int:
    for index, step in enumerate(steps):
        print(f"yard.py: running {step.spelled()}", flush=True)
        try:
            completed = subprocess.run(step.argv, cwd=str(step.cwd),
                                       env=dict(os.environ, **step.env), check=False)
            code = completed.returncode
        except OSError as exc:
            print(f"yard.py: could not start {step.argv[0]}: {exc}", file=sys.stderr)
            code = -1
        if code == 0 or code in step.tolerated:
            if code:
                why = ("already finished" if code == HC_FINISHED
                       else "not known to hc")
                print(f"yard.py: exit {code} tolerated (the terminal is {why})")
            continue
        print(f"\nyard.py: STOPPED. Step {index + 1} of {len(steps)} exited {code}; "
              f"nothing was rolled back.", file=sys.stderr)
        done = steps[:index]
        print("Completed:" if done else "Completed: none", file=sys.stderr)
        for finished in done:
            print(f"  {finished.spelled()}", file=sys.stderr)
        if code == BOOTSTRAP_AGENT_NOT_STARTED and step.argv[-1].endswith("yard-bootstrap.sh"):
            # The contour is provisioned (status `ok` at step 11) and only the
            # agent failed to start; re-running the bootstrap would start
            # nothing, because its status short-circuits. Its log above names
            # the exact start line for the agent command in use.
            print("The yard is provisioned; only its agent did not start. Start "
                  "it by hand (the bootstrap's log above has the exact line):",
                  file=sys.stderr)
            print(f"  hc run --bg --no-parent --keep --name {shlex.quote(step.cwd.name)} "
                  f"--cwd {shlex.quote(str(step.cwd))} "
                  f"--label herdr.space=sp{secrets.token_hex(16)} "
                  f"--label herdr.tab=tb{secrets.token_hex(16)} -- bash -c "
                  f"\"export HEADING_OS_YARD=1{node_path_export()}; {DEFAULT_AGENT_CMD} '{FIRST_PROMPT}'\"",
                  file=sys.stderr)
            remaining_steps = steps[index + 1:]
        else:
            remaining_steps = steps[index:]
        if remaining_steps:
            print("To finish by hand, once the cause is fixed:", file=sys.stderr)
        for remaining in remaining_steps:
            print(f"  {remaining.spelled()}", file=sys.stderr)
        return EXIT_PARTIAL
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("action", choices=("create", "remove"))
    parser.add_argument("branch")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the commands; run nothing but read-only git")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_ARGS if exc.code else EXIT_OK

    refusal = branch_refusal(args.branch)
    if refusal:
        print(f"yard.py: {refusal}", file=sys.stderr)
        return EXIT_ARGS
    kind = backend(dict(os.environ))
    if kind is None:
        print("yard.py: neither HERDR_PANE_ID (Herdr TUI) nor HC_TERMINAL_ID "
              "(herdr-core) is set, so which Herdr runs this session is unknown. "
              "Run it from a Herdr pane or an hc terminal.", file=sys.stderr)
        return EXIT_ARGS

    if not args.dry_run:
        require_main_clone(__file__)
    try:
        engine = main_clone_path(ROOT)
    except CloneGuardError as exc:
        print(f"yard.py: the engine clone could not be resolved: {exc}", file=sys.stderr)
        return EXIT_ARGS
    branch = args.branch
    yard = None
    if kind == "core" and args.action == "create":
        if CODED_BRANCH_RE.match(branch):
            print(f"yard.py: the slug {branch!r} already carries a code (w<N>-); "
                  f"give the slug alone and the code is allocated", file=sys.stderr)
            return EXIT_ARGS
        try:
            branch = f"w{next_code_number(engine)}-{branch}"
        except YardCodeError as exc:
            print(f"yard.py: the next yard code could not be computed: {exc}",
                  file=sys.stderr)
            return EXIT_ARGS
        # MEASURED 2026-11-20 (HOS-392): a 36-character branch made the
        # bootstrap's `hc run --name <branch>` fail with "bad name ...: use
        # 1-32 of a-z 0-9 - _ . starting with a letter or digit", and the yard
        # came up with no agent. Refused here, before anything is touched.
        if not HC_TERMINAL_NAME_RE.fullmatch(branch):
            why = (f"the slug may have at most {32 - len(branch) + len(args.branch)} characters"
                   if len(branch) > 32 else "the slug may hold only a-z 0-9 - _ .")
            print(f"yard.py: the branch {branch!r} is not a valid herdr-core terminal "
                  f"name (1-32 of a-z 0-9 - _ ., starting with a letter or digit); {why}",
                  file=sys.stderr)
            return EXIT_ARGS
    elif args.action == "remove" and is_code(branch):
        if kind != "core":
            print(f"yard.py: a bare code ({branch}) is herdr-core only; under the "
                  f"Herdr TUI name the full branch", file=sys.stderr)
            return EXIT_ARGS
        try:
            # The registered checkout, not a rebuilt `yard_dir`: a worktree
            # moved with `git worktree move` is still the one the code names.
            yard, branch = yard_for_code(engine, branch)
        except YardCodeError as exc:
            print(f"yard.py: {exc}; nothing was touched", file=sys.stderr)
            return EXIT_ARGS
        yard = yard.resolve()
    if yard is None:
        yard = yard_dir(engine, branch)
    label = "DRY RUN, nothing executed" if args.dry_run else "executing"
    print(f"yard.py: {args.action} {branch} under {kind} ({label})")
    print(f"  branch:       {branch}\n  engine clone: {engine}\n  yard dir:     {yard}")

    if args.action == "create":
        steps = create_steps(kind, engine, branch)
        blocker = None
        if git_ok(engine, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"):
            blocker = f"branch {branch} already exists"
        elif yard.exists() or yard.resolve() in registered_worktrees(engine):
            blocker = f"the yard dir {yard} already exists"
        code = EXIT_EXISTS if blocker else EXIT_OK
    else:
        workspace = "<workspace_id from herdr workspace list>"
        blocker, code = None, EXIT_OK
        if not git_ok(engine, "merge-base", "--is-ancestor", branch, "main"):
            blocker, code = (f"branch {branch} is not merged into main "
                             f"(or does not exist)"), EXIT_UNMERGED
        elif not yard.is_dir():
            blocker, code = f"the yard dir {yard} does not exist", EXIT_EXISTS
        elif kind == "core":
            # Before the first `hc` call: `hc kill` would stop the agent and
            # `git worktree remove` would then refuse a dirty yard, leaving it
            # on disk without its agent. Read-only, so a dry run asks it too.
            dirt = yard_dirt(yard)
            print(f"  check:        git -C {shlex.quote(str(yard))} status --porcelain "
                  f"-> {'clean' if dirt == '' else 'NOT clean'}")
            if dirt != "":
                blocker, code = (f"the yard {yard} has uncommitted or untracked "
                                 f"changes ({dirt}); nothing was touched"), EXIT_EXISTS
        elif kind == "tui" and not args.dry_run:
            found = tui_workspace_for(yard)
            if found is None:
                blocker, code = f"no herdr workspace has {yard} as its checkout", EXIT_EXISTS
            else:
                workspace = found
        steps = remove_steps(kind, engine, branch, workspace, yard)
        if kind == "tui":
            steps.insert(0, Step(["herdr", "workspace", "list"], engine))

    if args.dry_run:
        print("Commands, in order:")
        for step in steps:
            print(f"  {step.spelled()}")
        if blocker:
            print(f"A real run would refuse: {blocker} (exit {code}).")
        return code
    if blocker:
        print(f"yard.py: refused: {blocker}", file=sys.stderr)
        return code
    if args.action == "remove" and kind == "tui":
        steps = steps[1:]            # the listing already ran, above
    return run_steps(steps)


if __name__ == "__main__":
    sys.exit(main())
