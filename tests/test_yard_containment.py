"""GUARANTEE: a worktree cannot write into the main clone, cannot reach the entry point of anything that runs a daemon, and cannot run git across the shared data overlay.

FAILURE B: the two repositories merging, or a worktree acting on something live.
Several guards hold different halves of it and none of them substitutes for
another, so each is driven here.

THE FIFTH IS THE RELEASE GATE, added 2026-09-22. A commit made in a yard without
the operator's word is the same failure by another door: it puts the yard's
unreviewed tree on a branch HELM merges. `check_release_gate` is driven as pure
code over SYNTHETIC transcript files built under `tmp_path`, never a real one,
and it writes no release record because `_release_log_path` refuses its default
under pytest.

HOW EACH IS DRIVEN. `scripts/utils/clone_guard.py` and
`scripts/lib/require-main-clone.sh` are the entry guard every daemon installer
sits behind, and both are driven over a REAL git worktree of a throwaway
repository built under `tmp_path`: a guarded script is run from the main clone
and again from the worktree, and the exit statuses are compared. The engine's own
guard files are imported and sourced by absolute path, so a change to either is
what these cases execute. `scripts/utils/overlay_write_guard.py` is driven in a
CHILD PROCESS whose `HEADING_OS_DATA` points at a throwaway directory, because
arming and disarming in this interpreter would reach the guard this very run is
standing behind.

ONE ENVIRONMENT FACT IS SUPPLIED RATHER THAN ESTABLISHED, and it is disclosed
rather than hidden. `check_yard_write_guard` in `.claude/hooks/_dispatch.py` is
driven as pure code over synthetic payloads with `_IN_A_YARD`, the module
constant recording whether this checkout is a worktree, set True. Establishing it
honestly would mean creating a worktree OF THIS REPOSITORY, which writes into the
live clone's worktree registry. It does not hollow the case out: a mutation that
makes `_yard_in_a_yard` answer False regardless still goes red, because the guard
reads the function and not the constant.

NO DAEMON IS TOUCHED. Nothing here starts, stops, installs, restarts, inspects or
enumerates a unit, timer, PID file or service, and nothing runs `systemctl` or
any `lific` subcommand. The daemon half of the guarantee is established by
showing that the ENTRY GUARD every daemon script sits behind refuses from a
worktree, which is the mechanism, rather than by running a daemon script.

THE SIXTH IS THE OVERLAY GUARD'S GUARD MODE, added 2026-09-22 under HOS-73. It
runs in a CHILD PROCESS too, and over a PRETEND WORLD: a copy of the guard under
a throwaway engine clone with a throwaway sibling overlay, so the module derives
the operator's data to a directory `tmp_path` owns. `_structural_overlay_root()`
reads its own `__file__` and nothing else, which is the property that makes that
honest rather than a mock. The pretend engine is a real git repository, because
the allowed half turns on `git ls-files` and an absent repository would make
`_caller_is_tracked()` answer True for everything.

THE SEVENTH IS THE SAME GUARD'S FOREIGN-TREE LAYER, added 2026-09-23 under
HOS-90, and it is failure B at its widest: not this workspace's two repositories
merging with each other, but a run here reaching into a repository belonging to
somebody else. It runs in the same child process over the same pretend world,
with invented neighbours beside the pretend engine. No real tree is read,
written, listed or stat-ed by it, and no real name appears in it.

THE EIGHTH IS THE OTHER TWO HALVES OF THAT BAN, added 2026-09-23 under HOS-93
and HOS-96: a repository nested INSIDE the engine checkout, which the seventh's
sibling rule cannot reach, and a mutating `git` SUBPROCESS, which every wrapper
in this guard is blind to because they wrap in-process primitives. Its allowed
half is the larger one and carries the weight: the four argv shapes measured in
the five tools that only clone and pull must all still run, or the ban severs
this workspace's ability to READ another repository while stopping nothing.
`git` and `gh` here are stub executables written under `tmp_path`, so an allowed
case spawns a real child and reaches no network.

THE NINTH IS A YARD'S AGENT CARRYING THE YARD MARKER, added 2026-09-27: the
wall against `herdr agent start` into a yard's pane, and the bootstrap's step
11, which must not type its launch line into a pane that already holds an
agent. Its section at the end of this file says how each is driven.

THE TENTH IS A CHECKOUT'S OWN HANDOFF, added 2026-09-28 under HOS-158: /prime and
/next in one checkout must never present a handoff written from another. It
copies `scripts/` and `config/` into two invented checkouts under `tmp_path`,
points both at one throwaway data root, and drives the real writer and both
real readers there. No real transcript, session id or overlay is touched.

NOT CLAIMED. This does not establish that any particular installer actually calls
the guard; `scripts/check-gate-integrity.py` owns coverage questions of that
shape. The neighbouring-worktree refusals, the heredoc reader and the read-tool
branch of the write guard are not exercised. For the overlay guard, REFUSE and
GUARD are driven and RECORD and the OFF escape hatch are not; nothing here
exercises the snapshot half, the child-spawn suspect list, or the module-copy
handover. Nothing asserts anything about what `herdr` does.

THE LAST CASE IN THIS FILE, the rate-limit counter's lock (HOS-119,
2026-09-24), is not containment; it sits here only because this file already
drives `.claude/hooks/_dispatch.py`.
"""
from __future__ import annotations

import getpass
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# MEASURED 2026-09-21 by driving each case by hand before this file existed.
REFUSED_EXIT = 2          # clone_guard and the shell helper both exit 2
CHILD_REFUSED_EXIT = 3    # the overlay child's own code for OverlayWriteRefused

# Floors, asserted OUTSIDE every loop. A loop over a case table that came back
# short passes while checking less than it claims.
BLOCKED_CASE_FLOOR = 52   # MEASURED 2026-10-07 (HOS-211): 52 payloads the write guard blocks
ALLOWED_CASE_FLOOR = 33   # and 33 it must let through

# The release gate's own two directions, counted 2026-09-22 when the mid-turn
# source was written. Same reason as the two floors above: a case table that
# came back short passes every loop below it while checking less than it claims.
MIDTURN_REFUSED_FLOOR = 7
MIDTURN_ALLOWED_FLOOR = 3

# An INVENTED session id. This repository is public and a real one identifies a
# transcript on the operator's machine.
FAKE_SESSION = "zzqx0000-0000-4000-8000-000000000000"

GUARDED_PY = """#!/usr/bin/env python3
import sys
sys.path.insert(0, {engine!r})
from scripts.utils.clone_guard import require_main_clone
require_main_clone(__file__)
print("ALLOWED")
"""

GUARDED_SH = """#!/usr/bin/env bash
source "{engine}/scripts/lib/require-main-clone.sh"
require_main_clone
echo "ALLOWED"
"""

OVERLAY_CHILD = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from scripts.utils import overlay_write_guard as guard
guard.arm(getattr(guard, "MODE_" + sys.argv[3].upper()))
try:
    Path(sys.argv[2]).write_text("ZZQX\\n", encoding="utf-8")
except guard.OverlayWriteRefused:
    sys.exit(3)
sys.exit(0)
"""


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, check=False, timeout=120)


@pytest.fixture(scope="module")
def checkouts(tmp_path_factory):
    """A throwaway main clone and a real worktree of it. Never this repository."""
    base = tmp_path_factory.mktemp("yard")
    main, yard = base / "main-clone", base / "worktree"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    _git(main, "config", "user.email", "probe@example.invalid")
    _git(main, "config", "user.name", "ZZQX probe")
    (main / "scripts").mkdir()
    (main / "scripts" / "guarded.py").write_text(
        GUARDED_PY.format(engine=str(ROOT)), encoding="utf-8")
    (main / "scripts" / "guarded.sh").write_text(
        GUARDED_SH.format(engine=str(ROOT)), encoding="utf-8")
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "guarded")
    added = _git(main, "worktree", "add", "-q", str(yard))
    if added.returncode != 0 or not (yard / ".git").is_file():
        pytest.fail(f"could not build a worktree to test against: {added.stderr}")
    return main, yard


def _run(script: Path, cwd: Path) -> int:
    runner = [sys.executable] if script.suffix == ".py" else ["bash"]
    return subprocess.run([*runner, str(script)], cwd=str(cwd),
                          capture_output=True, text=True, timeout=300).returncode


@pytest.mark.parametrize("script", ["guarded.py", "guarded.sh"])
def test_a_guarded_script_refuses_to_run_from_a_worktree(checkouts, script):
    """Both halves of the entry guard, against a real worktree. The Python and
    shell guards are two copies of one predicate because the shell one may run no
    external command, so each is asserted: a change made to one and not the other
    is the failure this pair exists to see."""
    main, yard = checkouts
    assert _run(yard / "scripts" / script, cwd=main.parent) == REFUSED_EXIT


@pytest.mark.parametrize("script", ["guarded.py", "guarded.sh"])
def test_the_same_script_still_runs_from_the_main_clone(checkouts, script):
    """The other direction. Without it, `exit 2` at the top of either guard
    would satisfy every refusal case above and break every honest installer."""
    main, _yard = checkouts
    assert _run(main / "scripts" / script, cwd=main.parent) == 0


@pytest.fixture
def dispatch(tmp_path_factory):
    """`.claude/hooks/_dispatch.py`, loaded by path, plus the roots it resolves.

    Function scope, and that is the repair rather than a style. The guard
    resolves the data roots again at call time, inside the test, after the
    conftest's autouse pin has pointed `HEADING_OS_DATA` at the test's
    `tmp_path`. Resolved at MODULE scope, as this fixture was until 2026-09-26,
    the roots were read before that pin, from whatever the process inherited.
    Where HELM's sibling overlay exists both reads land on it and the mismatch is
    invisible; on a runner with no overlay the module-scope read found nothing
    and all seven dependent cases errored, while the guard itself would have
    answered with the pinned root. MEASURED 2026-09-26 in a clone with no
    overlay beside it: 7 errors before, the same cases resolved after. Loading
    the module per test costs about 33 ms, measured the same day.

    The working directory handed to the guard is a NEUTRAL throwaway directory,
    under neither the main clone nor the overlay, and that is load-bearing rather
    than tidiness. MEASURED 2026-09-21: with the main clone as the cwd, every
    Bash payload was refused by the reaches-into-HELM branch before its own
    branch was consulted, so a break planted in the `git push` refusal SURVIVED a
    green run of this file. Each case must be refused for its own reason.
    """
    target = ROOT / ".claude" / "hooks" / "_dispatch.py"
    if not target.is_file():
        pytest.fail(f"the yard write guard is not at {target}")
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("heading_dispatch_under_test", target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from scripts.utils.clone_guard import main_clone_path
    helm = main_clone_path(module.WORKSPACE)
    roots = module._yard_data_roots(helm)
    if not roots:
        pytest.fail(
            f"the write guard resolved no data overlay root from {helm}, so the "
            f"cases below would assert nothing. Searched: the sibling of the "
            f"main clone, and get_data_root().")
    neutral = tmp_path_factory.mktemp("neutral-cwd")
    return module, helm, roots[0], neutral


def _cases(helm: Path, overlay: Path, tmp: Path):
    """The payloads, built from the roots the guard itself resolved. `tmp` is a
    directory belonging to no checkout of this repository."""
    home = Path.home()
    blocked = [
        ("Write", {"file_path": str(helm / "scripts" / "zzqx.py"), "content": "x"}),
        ("Bash", {"command": f"git -C {overlay} commit -m zzqx"}),
        ("Bash", {"command": f"cd {overlay} && git commit -m zzqx"}),
        ("Bash", {"command": "git push origin HEAD"}),
        ("Bash", {"command": "git worktree prune"}),
        ("Bash", {"command": f"rm -rf {helm}/scripts"}),
        # HOS-196: the same reaches, spelled through shell variables, the shapes
        # measured passing on 2026-10-04 before the guard expanded them.
        ("Bash", {"command": f"H={helm}; S={overlay}/zzqx; cd $S; git clone --mirror --no-local $H m.git"}),
        ("Bash", {"command": f"H={helm}; export D={overlay}/zzqx; git -C $H bundle create $D/z.bundle --all"}),
        ("Bash", {"command": f"S=$(python3 {ROOT}/scripts/data-path.py scratch); cd \"$S\" && git init"}),
        ("Bash", {"command": "cd $(mktemp -d) && git commit -m zzqx"}),
        # HOS-199 item 1: a redirect is dropped, and only its target: the `-C`
        # after it still reaches the overlay, as the shell reads it.
        ("Bash", {"command": f"git > {overlay}/zzqx.txt -C {overlay} show HEAD:zzqx"}),
        # HOS-199: the overlay's git directory is closed to every tool, because a
        # hook planted there runs in HELM on the next overlay commit.
        ("Write", {"file_path": f"{overlay}/.git/hooks/pre-commit", "content": "x"}),
        ("Edit", {"file_path": f"{overlay}/.git/config", "old_string": "a", "new_string": "b"}),
        *[("Bash", {"command": c}) for c in (
            f"cp zzqx {overlay}/.git/hooks/pre-commit", f"tee {overlay}/.git/hooks/post-commit",
            f"echo x > {overlay}/.git/hooks/pre-commit", f"git show HEAD:x > {overlay}/.git/config",
            f"git show HEAD:x > {overlay}/.git/hooks/pre-commit",
            f"cd {overlay} && echo x >.git/hooks/pre-commit")],
        # HOS-211: places outside every checkout whose content decides what runs,
        # HELM included. The first eight are HELM's probes of 2026-10-07.
        ("Write", {"file_path": f"{home}/.claude/settings.json", "content": "x"}),
        ("Edit", {"file_path": f"{home}/.claude/settings.json", "old_string": "a", "new_string": "b"}),
        ("Write", {"file_path": f"{home}/.claude/hooks/zzqx.py", "content": "x"}),
        ("Write", {"file_path": f"{home}/.config/systemd/user/zzqx.service", "content": "x"}),
        ("Edit", {"file_path": f"{home}/.bashrc", "old_string": "a", "new_string": "b"}),
        ("MultiEdit", {"file_path": f"{home}/.profile", "edits": []}),
        ("NotebookEdit", {"notebook_path": f"{home}/.claude/zzqx.ipynb", "new_source": "x"}),
        ("Write", {"file_path": "~/.claude.json", "content": "x"}),
        *[("Write", {"file_path": f"{home}/{rel}", "content": "x"}) for rel in (
            ".local/bin/zzqx", ".gitconfig", ".config/herdr/config.toml",
            ".hindsight/zzqx.json", ".config/environment.d/zzqx.conf")],
        *[("Bash", {"command": c}) for c in (
            "echo x >> ~/.bashrc", f"cp zzqx {home}/.config/systemd/user/zzqx.service",
            "echo x > ~/.claude/settings.json", "echo x >> $HOME/.zshrc",
            "cd ~ && echo x >> .bashrc", "cd ~/.config/systemd/user && cp /zzqx/a.service .",
            "cd ~/.config/systemd/user && rm -f $ZZQX_UNSET",
            "ln -s /zzqx ~/.config/systemd/user/zzqx.service", "sed -i s/a/b/ ~/.bashrc",
            "sudo tee /etc/systemd/system/zzqx.service", f"cp zzqx ~{getpass.getuser()}/.profile",
            # changing what runs without naming a file
            "systemctl --user stop zzqx.service", "systemctl --user daemon-reload",
            "loginctl enable-linger", "crontab -r", "git config --global alias.zzqx status",
            f"git -C {ROOT} config core.hooksPath /dev/null",
            # read-only verbs in their writing forms, into HELM
            f"sed -i s/a/b/ {helm}/zzqx.txt", f"find {helm}/zzqx -delete",
            f"sort -o {helm}/zzqx.txt zzqx")],
    ]
    # The yard's own checkout: ROOT inside a yard, `tmp` in HELM (ROOT is HELM there).
    # MEASURED 2026-10-07: with ROOT, four own-tree cases failed in HELM, passed in a yard.
    own = tmp if ROOT.resolve() == Path(helm).resolve() else ROOT
    allowed = [
        ("Write", {"file_path": str(overlay / "zzqx.md"), "content": "x"}),
        ("Bash", {"command": f"ls {overlay}"}),
        ("Bash", {"command": f"git -C {overlay} status"}),
        ("Read", {"file_path": str(helm / "CLAUDE.md")}),
        ("Bash", {"command": "for f in zzqx-a zzqx-b; do rm $f; done"}),
        ("Bash", {"command": f"S=$(python3 {ROOT}/scripts/data-path.py scratch); cd $S && ls"}),
        # HOS-199 item 1: the shell writes git's output there; git never touches the overlay.
        ("Bash", {"command": f"git show HEAD:zzqx > {overlay}/zzqx.txt"}),
        ("Bash", {"command": f"S={overlay}; git diff >$S/zzqx.patch 2>>$S/zzqx.err"}),
        # HOS-199: keeping a search OUT of the git directory is not reaching it.
        ("Bash", {"command": f"cd {overlay} && grep -rn zzqx --exclude-dir=.git . && find . -path ./.git -prune -o -name zzqx"}),
        # HOS-211: reading those places stays open, and so do the yard's own
        # tree, scratch, the tool caches and the plan file plan mode asks for.
        ("Write", {"file_path": str(own / "zzqx.py"), "content": "x"}),
        ("Write", {"file_path": f"{home}/.cache/uv/zzqx", "content": "x"}),
        ("Write", {"file_path": f"{home}/.claude/plans/zzqx.md", "content": "x"}),
        ("Read", {"file_path": f"{home}/.claude/settings.json"}),
        *[("Bash", {"command": c}) for c in (
            "cat ~/.bashrc", f"grep -n zzqx {home}/.config/systemd/user/zzqx.service",
            "ls ~/.claude/projects", f"cat ~/.gitconfig > {overlay}/zzqx.txt",
            "rm -rf ~/.cache/pre-commit/zzqx", "systemctl --user status zzqx.service",
            "systemctl --user list-timers", "crontab -l",
            # reads of the config HELM shares, judged where it is shared
            *(f"git -C {own} config {v}" for v in ("--get user.email", "get user.email", "--list")),
            f"git -C {tmp} config user.email zzqx@example.invalid",
            f"sed -n 1p {helm}/CLAUDE.md", f"find {helm}/scripts -name zzqx",
            # shapes the HOS-196 corpus replay found refused by a first version
            "echo x > ~/.claude/jobs/zzqx/tmp/out.txt", "du -sh ~/.claude/projects",
            "cd ~/.claude/projects && grep -rl zzqx . | python3 -c 'import sys' 2>&1",
            "crontab -l 2>/dev/null", f"cp ~/.hindsight/zzqx.jsonl {overlay}/zzqx.jsonl",
            "cd ~/.claude && python3 - < zzqx.py")],
    ]
    return blocked, allowed


def test_a_yard_is_refused_the_writes_that_reach_helm_or_the_shared_index(
        dispatch, monkeypatch, tmp_path):
    """Payloads each reaching something live: four through a shell variable or
    substitution (HOS-196), eight into the data repository's git directory
    (HOS-199), thirty-three into a place outside every checkout that decides what
    runs, or a command that changes it without naming a file (HOS-211), one deletion of HELM spelled through a variable put to the
    deletion guard (HOS-199), and one hook planted through a `gitdir:` pointer
    file (HOS-199). The observable
    consequence is the decision object the hook returns, never the wording of
    the reason."""
    module, helm, overlay, neutral = dispatch
    blocked, _allowed = _cases(helm, overlay, neutral)
    assert len(blocked) >= BLOCKED_CASE_FLOOR, (
        f"{len(blocked)} refusal cases, below the {BLOCKED_CASE_FLOOR} measured "
        f"2026-10-04")
    monkeypatch.setattr(module, "_IN_A_YARD", True)
    survivors = [inp for tool, inp in blocked
                 if module.check_yard_write_guard(
                     {"tool_name": tool, "cwd": str(neutral), "tool_input": inp}) is None]
    assert not survivors, f"the yard write guard permitted {len(survivors)} of these"
    # HOS-199 item 2: the DELETION guard judges the expanded text too. Its HELM
    # clause is unconditional, so no yard state is consulted; the guarded set is
    # emptied so nothing on this machine is inspected. With the literal walk
    # this resolved `<neutral>/$H`, named no checkout and passed.
    monkeypatch.setattr(module, "_yard_guarded_checkouts", list)
    erase = {"tool_name": "Bash", "cwd": str(neutral),
             "tool_input": {"command": f"H={helm}; rm -rf $H"}}
    assert module.check_yard_deletion_guard(erase) is not None, (
        "the yard deletion guard let `rm -rf $H` through with H set to HELM")
    # HOS-199: a data root whose `.git` is a `gitdir:` FILE. The directory it
    # names is closed too, not only the literal `<root>/.git`, and the root is
    # the SECOND one, so every root `_yard_data_roots` returns is covered.
    store = tmp_path / "store"
    (tmp_path / ".git").write_text(f"gitdir: {store}\n", encoding="utf-8")
    monkeypatch.setattr(module, "_yard_data_roots", lambda _helm: [overlay, tmp_path])
    hooked = {"tool_name": "Write", "cwd": str(neutral),
              "tool_input": {"file_path": str(store / "hooks" / "pre-commit")}}
    assert module.check_yard_write_guard(hooked) is not None, (
        "the yard write guard let a hook be planted in a gitdir named by a pointer file")


def test_the_work_a_yard_is_supposed_to_do_is_still_permitted(dispatch, monkeypatch):
    """The other direction. Writing FILES into the data overlay from a worktree
    is the point of the arrangement, and reading HELM is how a task sees the code
    it branched from. A guard that refuses these is one that gets switched off."""
    module, helm, overlay, neutral = dispatch
    _blocked, allowed = _cases(helm, overlay, neutral)
    assert len(allowed) >= ALLOWED_CASE_FLOOR, (
        f"{len(allowed)} honest cases, below the {ALLOWED_CASE_FLOOR} measured "
        f"2026-10-04")
    monkeypatch.setattr(module, "_IN_A_YARD", True)
    refused = [inp for tool, inp in allowed
               if module.check_yard_write_guard(
                   {"tool_name": tool, "cwd": str(neutral), "tool_input": inp}) is not None]
    assert not refused, f"the yard write guard refused {len(refused)} honest calls"


def test_this_wall_stays_silent_in_the_main_clone(dispatch, monkeypatch):
    """HELM is where the live work happens, so the guard must not fire here."""
    module, helm, overlay, neutral = dispatch
    blocked, _allowed = _cases(helm, overlay, neutral)
    monkeypatch.setattr(module, "_IN_A_YARD", False)
    tool, inp = blocked[0]
    assert module.check_yard_write_guard(
        {"tool_name": tool, "cwd": str(neutral), "tool_input": inp}) is None


def _overlay_child(tmp_path: Path, target: Path, mode: str, data_root: Path) -> int:
    child = tmp_path / "overlay_child.py"
    child.write_text(OVERLAY_CHILD, encoding="utf-8")
    env = dict(os.environ, HEADING_OS_DATA=str(data_root))
    env.pop("HEADING_OS_OVERLAY_GUARD", None)
    return subprocess.run(
        [sys.executable, str(child), str(ROOT), str(target), mode],
        capture_output=True, text=True, env=env, cwd=str(tmp_path),
        timeout=300).returncode


def test_a_write_under_the_data_root_is_refused_rather_than_swept_in(tmp_path):
    """The sweep half: one write into a watched overlay root, refused at the
    primitive, so the bytes are never written and the traceback names the caller."""
    overlay = tmp_path / "overlay"
    (overlay / "crm").mkdir(parents=True)
    target = overlay / "crm" / "zzqx.md"
    assert _overlay_child(tmp_path, target, "refuse", overlay) == CHILD_REFUSED_EXIT
    assert not target.exists(), "the guard raised and the bytes landed anyway"


def test_an_ordinary_write_outside_the_data_root_still_lands(tmp_path):
    """The other direction. A guard that refuses every write refuses the suite."""
    overlay = tmp_path / "overlay"
    (overlay / "crm").mkdir(parents=True)
    target = tmp_path / "elsewhere.md"
    assert _overlay_child(tmp_path, target, "refuse", overlay) == 0
    assert target.exists()


def _transcript(tmp_path: Path, name: str, turn_prompt: str,
                *, mid: str | None = None, stale: str | None = None) -> Path:
    """One synthetic transcript, in the record shapes the harness really writes.

    MEASURED 2026-09-22 over the ten largest transcripts on this machine: a
    mid-turn message leaves an `enqueue` line and a `remove` line carrying
    `reason: "absorbed_mid_turn"` and never a `type: "user"` record, and a
    `queue-operation` record carries exactly `type`, `operation`, `content`,
    `sessionId` and `timestamp`, plus `reason` on a removal. `stale` is the same
    pair written BEFORE the turn's own prompt, which is how a word from an
    earlier turn reaches a reader that does not stop at a boundary.
    """
    lines: list[str] = []

    def rec(obj: dict) -> None:
        lines.append(json.dumps(obj, ensure_ascii=False))

    def queued(text: str, at: str) -> None:
        rec({"type": "queue-operation", "operation": "enqueue", "content": text,
             "sessionId": FAKE_SESSION, "timestamp": at})
        rec({"type": "queue-operation", "operation": "remove",
             "reason": "absorbed_mid_turn", "content": text,
             "sessionId": FAKE_SESSION, "timestamp": at})

    if stale is not None:
        queued(stale, "2026-09-22T09:00:00.000Z")
    rec({"type": "user", "promptSource": "typed", "sessionId": FAKE_SESSION,
         "message": {"role": "user", "content": turn_prompt}})
    rec({"type": "last-prompt", "lastPrompt": turn_prompt,
         "sessionId": FAKE_SESSION})
    rec({"type": "assistant", "sessionId": FAKE_SESSION,
         "message": {"role": "assistant", "content": []}})
    rec({"type": "user", "sessionId": FAKE_SESSION,
         "message": {"role": "user",
                     "content": [{"type": "tool_result", "content": "ok"}]}})
    if mid is not None:
        queued(mid, "2026-09-22T09:05:00.000Z")
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_the_release_gate_hears_the_operator_mid_turn_and_no_one_else(
        dispatch, tmp_path):
    """The fifth guard, both directions.

    PRE-FIX MEASUREMENT, 2026-09-22 on a live HELM transcript: the operator
    wrote mid-turn three times and not one of the three became a
    `type: "user"` record, so the gate, which reads those records and asks them
    for `promptSource`, refused a commit he had just asked for. All three
    acceptance cases below are red against the version before this change.

    The refusal half is what makes the acceptance half safe to have at all. A
    queue line carries no provenance field, so a brief delivered into a BUSY
    session arrives in the same shape as a typed word, and a subagent's
    hand-back arrives there too. Nothing here asserts the wording of a refusal:
    the observable consequence is the decision object the gate returns.
    """
    module, _helm, _overlay, _neutral = dispatch
    brief = (module._BRIEF_MARKER
             + "\nСделай правку и закоммить её, ветка готова.")
    commit = "git commit -m zzqx"
    push = "git push origin HEAD"

    refused = [
        ("nothing-was-typed-mid-turn", commit, None, None),
        ("the-mid-turn-line-is-a-marked-brief", commit,
         module._BRIEF_MARKER + "\nзакоммить это", None),
        ("a-subagents-hand-back", commit,
         '<agent-message from="zzqx">закоммить и запушить</agent-message>', None),
        ("a-task-notification", commit,
         "<task-notification>\n<task-id>zzqx</task-id>\nзакоммить\n"
         "</task-notification>", None),
        ("another-sessions-message", commit,
         '<cross-session-message from="zzqx">закоммить</cross-session-message>',
         None),
        ("the-word-sits-behind-the-turn-boundary", commit, None,
         "закоммить это"),
        ("the-mid-turn-word-forbids-it", commit, "не коммить пока", None),
    ]
    allowed = [
        ("russian", commit, "закоммить, пожалуйста"),
        ("english", commit, "commit it"),
        ("a-push-asked-for-mid-turn", push, "запушить ветку"),
    ]
    assert len(refused) >= MIDTURN_REFUSED_FLOOR, (
        f"{len(refused)} refusal cases, below the {MIDTURN_REFUSED_FLOOR} "
        f"counted 2026-09-22")
    assert len(allowed) >= MIDTURN_ALLOWED_FLOOR, (
        f"{len(allowed)} acceptance cases, below the {MIDTURN_ALLOWED_FLOOR} "
        f"counted 2026-09-22")

    def decide(name: str, command: str, mid: str | None, stale: str | None):
        path = _transcript(tmp_path, f"{name}.jsonl", brief, mid=mid, stale=stale)
        return module.check_release_gate(
            {"tool_name": "Bash", "tool_input": {"command": command},
             "transcript_path": str(path)})

    survivors = [name for name, command, mid, stale in refused
                 if decide(f"refuse-{name}", command, mid, stale) is None]
    assert not survivors, f"the release gate released on {survivors}"

    deaf = [name for name, command, mid in allowed
            if decide(f"allow-{name}", command, mid, None) is not None]
    assert not deaf, f"the release gate did not hear the operator on {deaf}"


def _write_records(path: Path, records: list[dict]) -> Path:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                            for r in records), encoding="utf-8")
    return path


def _typed(text: str) -> dict:
    return {"type": "user", "promptSource": "typed", "sessionId": FAKE_SESSION,
            "message": {"role": "user", "content": text}}


def _capped(text: str) -> dict:
    return {"type": "last-prompt", "lastPrompt": text, "sessionId": FAKE_SESSION}


def _enqueue(text: str) -> dict:
    return {"type": "queue-operation", "operation": "enqueue", "content": text,
            "sessionId": FAKE_SESSION, "timestamp": "2026-09-25T09:05:00.000Z"}


def _late_capped_gate(module, monkeypatch, path: Path, command: str,
                      arrives: dict | None):
    """Drive the gate with the capped record appended DURING its wait, if at all.

    The arrival is tied to the wall's own sleep rather than to a thread and a
    clock: the first time it sleeps, `arrives` is appended, which is the order
    the harness produces (the gate reads, the flush lands, the gate reads
    again). Nothing here reads the host clock, so the case is deterministic.
    """
    monkeypatch.setattr(module, "_RELEASE_WAIT_BUDGET_S", 0.05, raising=False)
    monkeypatch.setattr(module, "_RELEASE_WAIT_INTERVAL_S", 0.01, raising=False)
    pending = [arrives] if arrives is not None else []
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        while pending:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(pending.pop(), ensure_ascii=False) + "\n")

    monkeypatch.setattr(module.time, "sleep", sleep)
    decision = module.check_release_gate(
        {"tool_name": "Bash", "tool_input": {"command": command},
         "transcript_path": str(path)})
    return decision, len(slept)


# Counted 2026-09-25 when the wait was written, for the same reason as the
# floors above.
LATE_CAPPED_REFUSED_FLOOR = 4
LATE_CAPPED_ALLOWED_FLOOR = 3


def test_the_release_gate_waits_for_the_turns_capped_record_and_no_longer(
        dispatch, tmp_path, monkeypatch):
    """The capped record of THIS turn, arriving while the wall waits, authorises;
    one that never arrives, or arrives disagreeing, still refuses.

    PRE-FIX MEASUREMENT, 2026-09-25, Claude Code 2.1.282. The harness writes the
    capped `last-prompt` record only as part of a metadata re-append, which fires
    once roughly 32 KiB have been appended since the last one, and appends are
    flushed on a 100 ms timer. A PreToolUse hook starts BEFORE its own tool_use
    line is flushed: over five sequential probes in a yard the line landed 86 to
    105 ms into the hook, or was already there, and nothing more was appended
    for the rest of a 3 s window. Over the 25 largest transcripts on this
    machine, 1752 turns holding a tool call: the capped record preceded the first
    tool_use in 962, directly followed it inside that same flush in 134, landed
    only after the first tool RESULT in 589, and never in the turn in 55. The
    134 are the case a wait inside the hook can reach, and before this change
    the wall read once and refused them. The 589 no wait can reach, because the
    bytes that trigger the write come from the tool the hook is holding back.

    The acceptance half is red on the version before this change: it read the
    transcript once, found the previous turn's capped record (or none), and
    refused. The refusal half is what makes waiting safe: a typed record whose
    capped twin never comes is exactly what a forged one looks like.
    """
    module, _helm, _overlay, _neutral = dispatch
    commit = "git commit -m zzqx"
    word = "закоммить, пожалуйста"
    earlier = "посмотри, что там в логах"

    refused = [
        ("typed-and-no-capped-record-ever", [_typed(word)], None),
        ("the-previous-turns-capped-record-never-updates",
         [_typed(earlier), _capped(earlier), _typed(word)], None),
        ("a-capped-record-arrives-and-is-not-a-prefix",
         [_typed(earlier), _capped(earlier), _typed(word)],
         _capped("zzqx something else entirely")),
        ("the-capped-record-that-arrives-forbids-it",
         [_typed("не коммить пока")], _capped("не коммить пока")),
    ]
    allowed = [
        ("no-capped-record-yet-then-this-turns",
         [_typed(word)], _capped(word)),
        ("the-previous-turns-capped-record-then-this-turns",
         [_typed(earlier), _capped(earlier), _typed(word)], _capped(word)),
        ("a-slash-commands-capped-record-then-this-turns",
         _after_compact(earlier, after=[_typed(word)]), _capped(word)),
    ]
    assert len(refused) >= LATE_CAPPED_REFUSED_FLOOR, (
        f"{len(refused)} refusal cases, below the {LATE_CAPPED_REFUSED_FLOOR} "
        f"counted 2026-09-25")
    assert len(allowed) >= LATE_CAPPED_ALLOWED_FLOOR, (
        f"{len(allowed)} acceptance cases, below the {LATE_CAPPED_ALLOWED_FLOOR} "
        f"counted 2026-09-25")

    def decide(name: str, records: list[dict], arrives: dict | None):
        path = _write_records(tmp_path / f"{name}.jsonl", records)
        return _late_capped_gate(module, monkeypatch, path, commit, arrives)

    survivors = [name for name, records, arrives in refused
                 if decide(f"refuse-{name}", records, arrives)[0] is None]
    assert not survivors, f"the release gate released on {survivors}"

    outcomes = {name: decide(f"allow-{name}", records, arrives)
                for name, records, arrives in allowed}
    deaf = [name for name, (decision, _n) in outcomes.items() if decision is not None]
    assert not deaf, f"the release gate did not wait for the record on {deaf}"
    # The record lands at the first sleep, so the wall must stop there: one
    # re-read past agreement is latency on every early commit, for nothing.
    lingered = {name: n for name, (_d, n) in outcomes.items() if n != 1}
    assert not lingered, f"the wall kept waiting after the records agreed: {lingered}"

    # One read can be slow (the whole-file fallback took 3114 ms on a 306 MB
    # transcript, MEASURED 2026-09-25), so the CLOCK bounds the wait and not
    # only the count, or twenty slow reads outlast the hook's own timeout. The
    # clock here is the module's, advanced ten seconds per look.
    ticks = iter(range(0, 10 ** 6, 10))
    monkeypatch.setattr(module.time, "monotonic", lambda: float(next(ticks)))
    decision, reads = decide("slow-reads", [_typed(word)], None)
    assert decision is not None and reads == 1, (
        f"with every read outlasting the budget the wall read {reads} times")


def test_nothing_waits_but_a_release_whose_records_disagree(
        dispatch, tmp_path, monkeypatch):
    """The wait costs nothing anywhere else. A call that is not a commit or a
    push never sleeps, even over the one transcript shape that WOULD make a
    release wait, because the wait sits behind `release_action`. And a release
    whose two records already agree is decided on the first read."""
    module, _helm, _overlay, _neutral = dispatch
    in_flight = _write_records(tmp_path / "not-a-release.jsonl", [_typed("zzqx")])
    word = "закоммить, пожалуйста"
    agreed = _write_records(tmp_path / "agreed.jsonl", [_typed(word), _capped(word)])
    assert module.release_action("git status") is None

    def sleep(_seconds: float) -> None:
        raise AssertionError("the gate slept")

    monkeypatch.setattr(module.time, "sleep", sleep)
    assert module.check_release_gate(
        {"tool_name": "Bash", "tool_input": {"command": "git status"},
         "transcript_path": str(in_flight)}) is None
    assert module.check_release_gate(
        {"tool_name": "Bash", "tool_input": {"command": "git commit -m zzqx"},
         "transcript_path": str(agreed)}) is None


def _after_compact(prompt_before: str, *, mid: str | None = None,
                   after: list[dict] | None = None) -> list[dict]:
    """A turn compacted by a slash command, in the record shapes the harness
    writes. MEASURED 2026-09-25 over the 25 largest transcripts on this machine:
    the command leaves a user record whose `message.content` is the command
    line as a string with NO `promptSource`, then a capped record carrying the
    same line, a `compact_boundary`, and the summary as a string user record.
    No typed record is written for it, so the newest typed record is the prompt
    from BEFORE the command."""
    records = [
        _typed(prompt_before), _capped(prompt_before),
        {"type": "user", "sessionId": FAKE_SESSION,
         "message": {"role": "user", "content": "/compact"}},
        _capped("/compact"),
        {"type": "system", "subtype": "compact_boundary", "sessionId": FAKE_SESSION},
        {"type": "user", "isCompactSummary": True, "sessionId": FAKE_SESSION,
         "message": {"role": "user", "content": "zzqx summary of the session"}},
    ]
    records += after or []
    if mid is not None:
        records.append(_enqueue(mid))
    return records


COMMAND_REFUSED_FLOOR = 6
COMMAND_ALLOWED_FLOOR = 2


def test_after_a_slash_command_the_gate_hears_the_mid_turn_word_and_nothing_else(
        dispatch, tmp_path, monkeypatch):
    """A capped slash-command line newer than every typed record is a turn
    boundary for the typed reader, never a mismatch to refuse on sight.

    PRE-FIX MEASUREMENT, 2026-09-25, over the 25 largest transcripts on this
    machine: 4064 of 19094 capped records are slash-command lines, 3601 of them
    `/compact`, and at 11762 of 60838 tool_use records the newest capped record
    was a slash-command line that disagreed with the newest typed record. The
    typed reader returned `mismatch` there and the gate refused before it ever
    asked the mid-turn reader, so an operator's word typed mid-turn after a
    compaction, which lands only as a queue line, could never authorise. The
    acceptance half is red against the version before this change.

    What must still refuse: a typed record appended after the command with no
    capped twin, which is the shape of a forgery, and every word the mid-turn
    reader already refuses. A capped line that merely starts with a slash, such
    as a path, is not a command.
    """
    module, _helm, _overlay, _neutral = dispatch
    commit = "git commit -m zzqx"
    push = "git push origin HEAD"
    before = "посмотри, что там в логах"

    refused = [
        ("nothing-typed-mid-turn", commit, _after_compact(before)),
        ("a-typed-record-with-no-capped-twin-after-the-command", commit,
         _after_compact(before, after=[_typed("закоммить")])),
        ("the-mid-turn-line-is-a-marked-brief", commit,
         _after_compact(before, mid=module._BRIEF_MARKER + "\nзакоммить это")),
        ("a-subagents-hand-back", commit,
         _after_compact(before, mid='<agent-message from="zzqx">закоммить'
                                    '</agent-message>')),
        ("the-mid-turn-word-forbids-it", commit,
         _after_compact(before, mid="не коммить пока")),
        ("a-capped-path-is-not-a-command", commit,
         [_typed(before), _capped("/zzqx/some/path закоммить"),
          _enqueue("закоммить")]),
    ]
    allowed = [
        ("commit-typed-mid-turn", commit, _after_compact(before, mid="закоммить")),
        ("push-typed-mid-turn", push, _after_compact(before, mid="push it")),
    ]
    assert len(refused) >= COMMAND_REFUSED_FLOOR, (
        f"{len(refused)} refusal cases, below the {COMMAND_REFUSED_FLOOR} "
        f"counted 2026-09-25")
    assert len(allowed) >= COMMAND_ALLOWED_FLOOR, (
        f"{len(allowed)} acceptance cases, below the {COMMAND_ALLOWED_FLOOR} "
        f"counted 2026-09-25")

    def decide(name: str, command: str, records: list[dict]):
        path = _write_records(tmp_path / f"{name}.jsonl", records)
        return _late_capped_gate(module, monkeypatch, path, command, None)[0]

    survivors = [name for name, command, records in refused
                 if decide(f"refuse-{name}", command, records) is None]
    assert not survivors, f"the release gate released on {survivors}"

    deaf = [name for name, command, records in allowed
            if decide(f"allow-{name}", command, records) is not None]
    assert not deaf, f"the release gate did not hear the operator on {deaf}"


# A registry carrying the journal's row, and one carrying a `names:` block that
# does not. The second is not "no registry": with no file at all the engine's own
# shipped defaults answer, which is the third case below.
JOURNAL_REGISTRY = """version: 1
names:
  release-journal:
    what: "every commit and push the release gate authorised"
    runtime: release/cache
    under: authorised.jsonl
services:
  release: "the release gate: the journal of every authorised commit and push"
"""
OTHER_REGISTRY = """version: 1
names:
  zzqx-elsewhere:
    what: "an address that is not the journal"
    path: zzqx/elsewhere.md
services:
  release: "the release gate: the journal of every authorised commit and push"
"""

# Written by the child, never by this interpreter: `_release_log_path` refuses
# its default whenever it can tell it is under pytest, which is the containment
# that keeps this file away from the operator's real trail. Driving the recorder
# honestly therefore means leaving pytest, and a subprocess is the only way out.
RECORD_CHILD = """
import importlib.util, sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root))
spec = importlib.util.spec_from_file_location(
    "dispatch_release_journal", root / ".claude" / "hooks" / "_dispatch.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod._record_release("commit", "git commit -m zzqx", "ZZQX invented authorisation")
"""

JOURNAL_REL = Path("runtime-junk") / "release" / "cache" / "authorised.jsonl"


def _record_in_child(tmp_path: Path, name: str, registry: str | None) -> tuple[Path, Path]:
    """Run the recorder outside pytest over a scratch HOME and a scratch data root.

    BOTH roots are scratch and both are passed explicitly. `HEADING_OS_DATA` is
    set rather than cleared: cleared, `_release_log_default` falls to this
    checkout's own `.env`, which on the operator's machine names the live
    overlay, and the case would append an invented record to his real trail.
    """
    case = tmp_path / name
    home, data = case / "home", case / "data"
    home.mkdir(parents=True)
    (data / "config").mkdir(parents=True)
    if registry is not None:
        (data / "config" / "data-paths.yaml").write_text(registry, encoding="utf-8")
    driver = case / "drive.py"
    driver.write_text(RECORD_CHILD, encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("PYTEST_")
           and k not in ("HEADING_OS_RELEASE_LOG", "HEADING_OS_DATA", "HOME")}
    env["HOME"], env["HEADING_OS_DATA"] = str(home), str(data)
    subprocess.run([sys.executable, str(driver), str(ROOT)], env=env,
                   capture_output=True, text=True, check=False, timeout=120)
    return home, data


def test_an_authorised_release_is_journalled_in_the_data_repository_never_in_home(
        tmp_path):
    """The audit trail's ADDRESS, both directions, driven as a real run.

    PRE-MOVE MEASUREMENT, 2026-09-22: `_release_log_default` did not exist and
    the default was the literal `Path.home() / ".heading-os" / "release" /
    "authorised.jsonl"`, so all three cases below are red against the previous
    version of the hook. The operator reversed that on 2026-09-22 (HOS-42) and
    refused the cadence copier the old comment block named as the only
    acceptable alternative, because a copy on a cadence is a second audit trail
    that diverges from the first.

    The third case is the one that makes the other two worth having. A tree
    whose registry cannot answer for the journal records NOTHING, rather than
    falling back to a home directory the engine no longer spells: a fallback is
    how a moved address keeps a second home nobody maintains. The observable
    consequence throughout is bytes on disk under two scratch roots, never a
    path the recorder was asked to compute.

    NOT CLAIMED. Nothing here says the operator's own registry carries the row,
    which is a fact about his overlay and not about this repository, and nothing
    here touches the journal his machine is really writing. Nor would it notice
    a regression to the address this log had before 2026-09-08, inside the
    checkout under `.claude/state/`: both scratch roots are watched and the
    checkout is not, so the paragraph above `_RELEASE_LOG_PIN` is still the only
    thing holding that one.
    """
    cases = [
        ("registry-names-it", JOURNAL_REGISTRY, True),
        ("engine-defaults-answer", None, True),
        ("registry-answers-for-other-things", OTHER_REGISTRY, False),
    ]
    assert len(cases) >= 3, f"{len(cases)} cases, below the 3 counted 2026-09-22"

    strays, missing, extra = [], [], []
    for name, registry, expected in cases:
        home, data = _record_in_child(tmp_path, name, registry)
        strays += [str(p) for p in home.rglob("authorised.jsonl")]
        written = data / JOURNAL_REL
        if expected and not written.is_file():
            missing.append(name)
        elif expected:
            lines = [json.loads(line) for line
                     in written.read_text(encoding="utf-8").splitlines() if line.strip()]
            if len(lines) != 1 or lines[0].get("action") != "commit":
                missing.append(f"{name} (wrote {len(lines)} usable record(s))")
        elif list(data.rglob("authorised.jsonl")):
            extra.append(name)

    assert not strays, f"the journal was written under a home directory: {strays}"
    assert not missing, f"no journal record reached the data repository for {missing}"
    assert not extra, f"an unresolvable address still wrote a journal for {extra}"


# The sixth guard, added 2026-09-22 under HOS-73. Floors for its own case table,
# asserted outside the loop like the four above.
ENTRYLESS_REFUSED_FLOOR = 4
ENTRYLESS_ALLOWED_FLOOR = 4

# The write the cases below attempt, loaded from the pretend engine BY PATH so
# that no `scripts` name is bound and nothing resolves against the real tree.
#
# `structural_only=True` is what `arm_process_wide()` passes, so it is the
# always-on configuration rather than a test-only one, and it is what every case
# but one uses. The exception is the case that supplies `HEADING_OS_DATA`: a run
# pointed at a scratch root has TWO watched roots, which is the only arrangement
# in which the guard has to tell the operator's overlay from a scratch one, and
# telling them apart is what stops the new rule firing on the very remedy the old
# one recommends.
ENTRYLESS_BODY = """
import sys, os, importlib.util
ENGINE = os.environ["HOS73_ENGINE"]
TARGET = os.environ["HOS73_TARGET"]
VIA = os.environ["HOS73_VIA"]
spec = importlib.util.spec_from_file_location(
    "_hos73", os.path.join(ENGINE, "scripts", "utils", "overlay_write_guard.py"))
g = importlib.util.module_from_spec(spec)
sys.modules["_hos73"] = g
spec.loader.exec_module(g)
g.arm(g.MODE_GUARD, structural_only="HEADING_OS_DATA" not in os.environ)
try:
    if VIA == "tool":
        sys.path.insert(0, ENGINE)
        import committed_tool
        committed_tool.write(TARGET)
    else:
        with open(TARGET, "w") as fh:
            fh.write("ZZQX\\n")
except g.OverlayWriteRefused:
    sys.exit(3)
sys.exit(0)
"""

ENTRYLESS_TOOL = """
def write(target):
    with open(target, "w") as fh:
        fh.write("ZZQX\\n")
"""


def _pretend_world(tmp_path: Path) -> tuple[Path, Path]:
    """An engine clone with a sibling overlay, both throwaway.

    `_structural_overlay_root()` derives the operator's overlay from the guard
    module's OWN location and from nothing else, which is the property that makes
    this possible: a copy of the module under `<world>/engine/scripts/utils/`
    answers `<world>/.heading-os-data`. So the guard under test believes the
    throwaway directory is the operator's live data, and every refusal below is
    the real branch refusing a real write, with no environment variable steering
    it and nothing anywhere near the operator's files.

    The engine copy is a REAL git repository, because the allowed half turns on
    `git ls-files`: with no repository `tracked_workspace_files()` answers None,
    `_caller_is_tracked()` answers True for everything, and the tracked-caller
    case would pass while establishing nothing.
    """
    world = tmp_path / "world"
    engine = world / "engine"
    (engine / "scripts" / "utils").mkdir(parents=True)
    (world / ".heading-os-data" / "crm").mkdir(parents=True)
    for name in ("overlay_write_guard.py", "excluded_dirs.py"):
        (engine / "scripts" / "utils" / name).write_bytes(
            (ROOT / "scripts" / "utils" / name).read_bytes())
    (engine / "committed_tool.py").write_text(ENTRYLESS_TOOL, encoding="utf-8")
    (engine / "tracked_caller.py").write_text(ENTRYLESS_BODY, encoding="utf-8")
    (engine / "untracked_caller.py").write_text(ENTRYLESS_BODY, encoding="utf-8")
    _git(engine, "init", "-q", ".")
    _git(engine, "add", "scripts/utils/overlay_write_guard.py",
         "scripts/utils/excluded_dirs.py", "committed_tool.py", "tracked_caller.py")
    return engine, world / ".heading-os-data"


def _entryless_child(engine: Path, form: str, target: Path, via: str,
                     session_root: Path | None = None) -> int:
    """Run the same program through one entry-point FORM. Returns the exit code.

    The forms are the subject. MEASURED 2026-09-22, one invocation per spelling:
    `python -` leaves `sys.argv[0]` as `-`, `python -c` leaves it `-c`, and a
    bare `python` reading a pipe leaves it the EMPTY STRING. Those are the three
    entry points with no file behind them; the fourth form is a real file,
    tracked or not. The program's arguments arrive through the environment
    because the bare-pipe form has no argument vector to put them in.
    `HEADING_OS_OVERLAY_GUARD` is cleared rather than set, so nothing here relies
    on the escape hatch; the copy under test is armed GUARD explicitly, which
    `resolve_mode()` honours over any environment value.
    """
    env = dict(os.environ, HOS73_ENGINE=str(engine), HOS73_TARGET=str(target),
               HOS73_VIA=via)
    env.pop("HEADING_OS_OVERLAY_GUARD", None)
    env.pop("HEADING_OS_DATA", None)
    if session_root is not None:
        env["HEADING_OS_DATA"] = str(session_root)
    if form == "stdin":
        cmd, stdin = [sys.executable, "-"], ENTRYLESS_BODY
    elif form == "dash-c":
        cmd, stdin = [sys.executable, "-c", ENTRYLESS_BODY], None
    elif form == "pipe":
        cmd, stdin = [sys.executable], ENTRYLESS_BODY
    else:
        cmd, stdin = [sys.executable, str(engine / form)], None
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                          env=env, cwd=str(engine), timeout=300).returncode


def test_a_program_with_no_file_behind_it_cannot_write_the_operators_overlay(tmp_path):
    """HOS-73. GUARD mode asked 'is the calling file tracked?', and a program fed
    to the interpreter on stdin has no calling file, so the question had no
    subject and the refusal branch never ran.

    MEASURED 2026-09-22 before the fix, on a throwaway overlay: the identical
    write landed ALLOWED from `python - <<'PY'` and from `python -c`, and REFUSED
    from an untracked file. The wall punished the careful form and passed the
    careless one, which is the shape a heredoc in an ordinary session takes.

    Failure B: the operator's live records are the LIVE thing here, and scratch
    code writing them unseen is the same failure as a yard acting on the main
    clone. Not a new file, per obligation 4; this one already holds failure B.

    BOTH HALVES, and the allowed half is the larger one. MEASURED the same day
    over the live journal's 263,256 records, 209,955 (79.75%) carry no caller
    frame at all, 196,356 of them innermost in `aiosqlite/core.py` reached from
    the tracked graph-memory pilot script (removed 2026-10-04, HOS-198). A rule
    that refused on a missing caller would refuse four writes in five. So the
    refusal turns on the ENTRY
    POINT having no file, which those 263,256 records show for 854 writes into
    the operator's overlay, 0.32%; and three things that must keep working are
    driven here beside the four that must not.
    """
    engine, overlay = _pretend_world(tmp_path)
    target = overlay / "crm" / "zzqx.md"
    scratch = tmp_path / "scratch.md"
    session = tmp_path / "session-root"
    (session / "crm").mkdir(parents=True)

    cases = [
        # (label, form, target, via, scratch HEADING_OS_DATA, must be refused)
        ("stdin writes the overlay", "stdin", target, "direct", None, True),
        ("-c writes the overlay", "dash-c", target, "direct", None, True),
        ("an untracked file writes the overlay",
         "untracked_caller.py", target, "direct", None, True),
        ("a bare `python` reading a pipe writes the overlay",
         "pipe", target, "direct", None, True),
        ("a tracked file writes the overlay",
         "tracked_caller.py", target, "direct", None, False),
        ("stdin drives committed tooling", "stdin", target, "tool", None, False),
        ("stdin writes outside any watched root",
         "stdin", scratch, "direct", None, False),
        # The remedy the guard itself recommends, and the only case in which two
        # roots are watched at once. Refusing this one would tell a probe to
        # point HEADING_OS_DATA somewhere safe and then refuse it for obeying.
        ("stdin writes a scratch HEADING_OS_DATA root",
         "stdin", session / "crm" / "zzqx.md", "direct", session, False),
    ]
    refused = [c for c in cases if c[5]]
    allowed = [c for c in cases if not c[5]]
    assert len(refused) >= ENTRYLESS_REFUSED_FLOOR
    assert len(allowed) >= ENTRYLESS_ALLOWED_FLOOR

    wrong, leaked = [], []
    for label, form, where, via, session_root, must_refuse in cases:
        if where.exists():
            where.unlink()
        code = _entryless_child(engine, form, where, via, session_root=session_root)
        if must_refuse:
            if code != CHILD_REFUSED_EXIT:
                wrong.append(f"{label}: exit {code}, wanted {CHILD_REFUSED_EXIT}")
            if where.exists():
                leaked.append(label)
        elif code != 0:
            wrong.append(f"{label}: exit {code}, wanted 0")
        elif not where.exists():
            leaked.append(f"{label}: allowed but nothing was written")

    assert not wrong, f"the wall answered wrongly: {wrong}"
    assert not leaked, f"bytes landed on the wrong side of the wall: {leaked}"


# The seventh guard, added 2026-09-23 under HOS-90. Floors for its own case
# table, asserted outside the loop like the five above.
FOREIGN_REFUSED_FLOOR = 4
FOREIGN_ALLOWED_FLOOR = 3


def _pretend_neighbours(world: Path) -> dict:
    """Trees beside the pretend engine: three foreign, two of this workspace's.

    EVERY NAME HERE IS INVENTED. The real slugs live in the operator's private
    overlay and this repository is public, so a fixture that borrowed one would
    publish a person. Nothing below is anybody.

    The three shapes the rule has to tell apart, which is the whole subject:

    * a repository beside the engine that is neither of the workspace's two,
      spelled two ways so the test cannot pass by matching one prefix;
    * a directory beside the engine that is NOT a repository, which is what the
      yards, the caches and the scratch directories are. Refusing those would
      refuse a yard its own checkout, so this is the expensive half to get
      wrong;
    * a WORKTREE of the engine sitting beside it. Its `.git` is a FILE naming
      the clone it belongs to, written here exactly as git writes one, and
      `_main_clone_root` is what reads it back. Today no worktree of this
      repository sits at that level, which is precisely why a test is the only
      thing that can hold the case.
    """
    layout = {
        "foreign_overlay": world / ".heading-os-data-vex-tallowmere",
        "foreign_published": world / "heading-os-tinwright",
        "foreign_third": world / "heading-os-quillmarrow",
        "not_a_repo": world / "scratchpad",
        "engine_worktree": world / "sidecheckout",
    }
    for key, tree in layout.items():
        (tree / "records").mkdir(parents=True)
        if key.startswith("foreign"):
            (tree / ".git").mkdir()
    (layout["engine_worktree"] / ".git").write_text(
        f"gitdir: {world / 'engine' / '.git' / 'worktrees' / 'sidecheckout'}\n",
        encoding="utf-8")
    # The operator's overlay IS a repository on the real machine, and the rule
    # excludes it by identity rather than by not finding a `.git`. Leaving the
    # pretend one bare would let the exclusion pass by accident, so it is marked
    # here rather than in `_pretend_world`, which the guard beside this one
    # shares. Bare on purpose: `_tracked_in` gets no answer from it and skips
    # it, so nothing about the tracked set moves for either guard.
    (world / ".heading-os-data" / ".git").mkdir(exist_ok=True)
    return layout


def test_a_run_here_cannot_write_into_a_repository_that_is_not_this_workspaces(tmp_path):
    """HOS-90. The workspace root holds the operator's two repositories and a
    number of trees that are not his to change, and nothing refused a write into
    any of them.

    MEASURED 2026-09-23 in the main clone, by walking the workspace root: 15
    immediate children, 8 of them git repositories, of which SIX are neither the
    engine nor the operator's overlay. `_watched_roots()` returned one entry.
    One root of seven, and the six were held by care rather than by a guard.

    Failure B: two repositories merging, a run reaching into a tree that is not
    its own. Not a new file, per obligation 4; this one already holds failure B
    and already holds this guard.

    THE REFUSAL IS AHEAD OF THE MODE BRANCHES, and the tracked-caller case below
    is why that had to be a separate layer rather than a sixth watched root.
    GUARD mode allows any write whose innermost workspace frame is a file git
    tracks, and every tool in this repository is tracked, so adding those trees
    to the watched set would have produced a guard that refuses nobody while
    reading, in `_watched_roots()`, exactly as though the gap had been closed.

    BOTH HALVES, and the allowed half is the one that would hurt. A yard is a
    worktree and a yard writes its own checkout on every edit; the rule is
    therefore three tests deep, and the last two are here to be refuted rather
    than described: a sibling that is not a repository stays writable, and a
    sibling that is a worktree of this engine stays writable.
    """
    engine, overlay = _pretend_world(tmp_path)
    world = engine.parent
    trees = _pretend_neighbours(world)

    cases = [
        # (label, form, target, via, must be refused)
        ("an invented foreign overlay", "stdin",
         trees["foreign_overlay"] / "records" / "zzqx.md", "direct", True),
        ("an invented published clone", "stdin",
         trees["foreign_published"] / "records" / "zzqx.md", "direct", True),
        # Deep inside, because the refusal is a prefix test and a rule that only
        # caught the top level would look identical from the outside.
        ("deep inside a third invented tree", "stdin",
         trees["foreign_third"] / "records" / "zzqx.md", "direct", True),
        # THE ONE THAT DECIDES THE DESIGN. Committed tooling is what GUARD mode
        # exists to allow, and it must not be allowed here.
        ("committed tooling writes an invented tree",
         "tracked_caller.py", trees["foreign_overlay"] / "records" / "zzqy.md",
         "direct", True),
        ("committed tooling writes the operator's own overlay",
         "tracked_caller.py", overlay / "crm" / "zzqx.md", "direct", False),
        ("a sibling that is not a repository", "stdin",
         trees["not_a_repo"] / "records" / "zzqx.md", "direct", False),
        ("a worktree of this engine beside it", "stdin",
         trees["engine_worktree"] / "records" / "zzqx.md", "direct", False),
    ]
    refused = [c for c in cases if c[4]]
    allowed = [c for c in cases if not c[4]]
    assert len(refused) >= FOREIGN_REFUSED_FLOOR
    assert len(allowed) >= FOREIGN_ALLOWED_FLOOR

    wrong, leaked = [], []
    for label, form, where, via, must_refuse in cases:
        if where.exists():
            where.unlink()
        code = _entryless_child(engine, form, where, via)
        if must_refuse:
            if code != CHILD_REFUSED_EXIT:
                wrong.append(f"{label}: exit {code}, wanted {CHILD_REFUSED_EXIT}")
            if where.exists():
                leaked.append(label)
        elif code != 0:
            wrong.append(f"{label}: exit {code}, wanted 0")
        elif not where.exists():
            leaked.append(f"{label}: allowed but nothing was written")

    assert not wrong, f"the wall answered wrongly: {wrong}"
    assert not leaked, f"bytes landed on the wrong side of the wall: {leaked}"


# The eighth guard, added 2026-09-23 under HOS-93 and HOS-96: the two halves of
# the ban that the seventh does not reach. Floors for its own case table.
NESTED_REFUSED_FLOOR = 5
NESTED_ALLOWED_FLOOR = 5

# Arms the guard, then spawns one command, and reports which side of the wall it
# landed on. The argv arrives as JSON through the environment for the same
# reason the body above takes its arguments that way: the bare-pipe entry point
# has no argument vector to put them in.
SPAWN_BODY = """
import sys, os, json, importlib.util, subprocess
ENGINE = os.environ["HOS93_ENGINE"]
ARGV = json.loads(os.environ["HOS93_ARGV"])
CWD = os.environ.get("HOS93_CWD") or None
spec = importlib.util.spec_from_file_location(
    "_hos93", os.path.join(ENGINE, "scripts", "utils", "overlay_write_guard.py"))
g = importlib.util.module_from_spec(spec)
sys.modules["_hos93"] = g
spec.loader.exec_module(g)
g.arm(g.MODE_GUARD, structural_only=True)
try:
    subprocess.Popen(ARGV, cwd=CWD).wait()
except g.OverlayWriteRefused:
    sys.exit(3)
sys.exit(0)
"""


def _pretend_nested_repo_and_stubs(engine: Path) -> tuple[Path, Path, Path]:
    """A repository nested INSIDE the pretend engine, and stub git/gh binaries.

    The nested repository is the shape of layer 2: a clone sitting inside the
    engine checkout rather than beside the workspace root, so the sibling rule
    cannot reach it. The name here is
    invented like every other name in this file; the rule under test knows no
    name and asks only whether an immediate child of the engine carries a
    `.git`.

    THE STUBS ARE WHY NO NETWORK IS TOUCHED. The allowed half has to prove that
    a clone and a pull still RUN, not merely that the guard returns: the refusal
    happens before the child exists, so an allowed case spawns a real child. A
    stub named `git` and a stub named `gh` give the argv its real basename,
    which is what `_spawn_is_inward` reads to decide how many words to skip,
    while the child itself does nothing at all.
    """
    nested = engine / ".hosted-clone"
    (nested / "records").mkdir(parents=True)
    (nested / ".git").mkdir()
    binroot = engine.parent / "bin"
    binroot.mkdir()
    stubs = {}
    for name in ("git", "gh"):
        stub = binroot / name
        stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        stub.chmod(0o755)
        stubs[name] = stub
    return nested, stubs["git"], stubs["gh"]


def _spawn_child(engine: Path, argv: list, cwd: Path | None) -> int:
    """Arm the guard in a child and have it spawn `argv`. Returns the exit code."""
    env = dict(os.environ, HOS93_ENGINE=str(engine),
               HOS93_ARGV=json.dumps([str(a) for a in argv]),
               HOS93_CWD="" if cwd is None else str(cwd))
    env.pop("HEADING_OS_OVERLAY_GUARD", None)
    env.pop("HEADING_OS_DATA", None)
    return subprocess.run([sys.executable, "-c", SPAWN_BODY], capture_output=True,
                          text=True, env=env, cwd=str(engine), timeout=300).returncode


def test_a_nested_clone_and_a_mutating_subprocess_cannot_change_a_foreign_tree(tmp_path):
    """HOS-93 and HOS-96. The seventh guard refuses in-process Python primitives
    into repositories BESIDE the workspace root. Two halves of the operator's
    instruction sat outside that.

    MEASURED 2026-09-23: three of the six tools he stopped reach another
    repository through `git` as a SUBPROCESS, each with `cwd` set to that
    repository, running `git add`, `git commit` and `git push`; the wrappers see
    in-process primitives only, so all three walked past. And a clone nested
    INSIDE the engine checkout, where the sibling rule cannot reach it, took
    writes that went through, 16 bytes, refused by nobody.

    Failure B at its widest, the same subject as the guard above it. Not a new
    file, per obligation 4.

    THE ALLOWED HALF IS THE LARGER ONE HERE AND THAT IS DELIBERATE. Five tools
    reach a foreign tree only to CLONE or PULL: the bytes travel inward and
    nothing in the other repository changes. Refusing those would sever every
    read-only clone and pull while serving the operator's instruction not at all. The
    guard still allows the four argv shapes measured in those five tools, so
    each is driven below and must still pass.
    """
    engine, overlay = _pretend_world(tmp_path)
    world = engine.parent
    trees = _pretend_neighbours(world)
    nested, git, gh = _pretend_nested_repo_and_stubs(engine)
    beside = trees["foreign_overlay"]

    spawns = [
        # (label, argv, cwd, must be refused)
        ("commit in a tree beside us", [git, "commit", "-m", "x"], beside, True),
        ("push from a tree beside us", [git, "push"], beside, True),
        ("add named through -C", [git, "-C", beside, "add", "-A"], None, True),
        ("commit inside the nested clone", [git, "commit", "-m", "x"], nested, True),
        # A one-word commit message that happens to read as a permitted verb.
        # The first draft of the rule accepted either of the first two words and
        # this is what talked past it.
        ("a commit message reading `clone`", [git, "commit", "-m", "clone"],
         beside, True),
        ("a copy into a tree beside us",
         ["/bin/cp", str(engine / "committed_tool.py"),
          str(beside / "records" / "zzqx.md")], None, True),
        # The four shapes measured in the five inward tools, 2026-09-23.
        ("gh repo clone into a tree beside us, for reading",
         [gh, "repo", "clone", "org/invented", beside], None, False),
        ("git pull in a tree beside us", [git, "pull"], beside, False),
        ("git pull --ff-only in the nested clone",
         [git, "pull", "--ff-only"], nested, False),
        ("git log in a tree beside us",
         [git, "log", "--since=48 hours ago"], beside, False),
        ("git status in our own engine", [git, "status"], engine, False),
    ]
    refused = [c for c in spawns if c[3]]
    allowed = [c for c in spawns if not c[3]]
    assert len(refused) >= NESTED_REFUSED_FLOOR
    assert len(allowed) >= NESTED_ALLOWED_FLOOR

    wrong = []
    for label, argv, cwd, must_refuse in spawns:
        code = _spawn_child(engine, argv, cwd)
        want = CHILD_REFUSED_EXIT if must_refuse else 0
        if code != want:
            wrong.append(f"{label}: exit {code}, wanted {want}")

    # Layer 2 on its own: an ordinary in-process write inside the nested clone,
    # from COMMITTED tooling, which is exactly what GUARD mode would allow.
    nested_target = nested / "records" / "zzqx.md"
    if nested_target.exists():
        nested_target.unlink()
    code = _entryless_child(engine, "tracked_caller.py", nested_target, "direct")
    if code != CHILD_REFUSED_EXIT:
        wrong.append(f"a write inside the nested clone: exit {code}, "
                     f"wanted {CHILD_REFUSED_EXIT}")
    leaked = [str(nested_target)] if nested_target.exists() else []

    assert not wrong, f"the wall answered wrongly: {wrong}"
    assert not leaked, f"bytes landed on the wrong side of the wall: {leaked}"


# ============================================================
# HOS-119: the runaway-write counter, and the lock under it
# ============================================================
#
# Not this file's guarantee. The module docstring says which one it is, why it
# is here anyway, and that the seam is the operator's to close.

# The fan-out shape, taken from the measurement the lock was written against:
# sixteen hook processes at once on one state file (`_rate_state_lock`, MEASURED
# 2026-09-04). Kept at sixteen so the two measurements are comparable.
RATE_FANOUT_WRITERS = 16

# Locked trials. One is enough when it completes without the fail-open firing;
# the rest exist only so a loaded machine gets another chance before the case
# gives up rather than lying. MEASURED 2026-09-24 on this machine, 20 locked
# trials: 20 counted 16 of 16 and none of the 320 acquisitions reached the
# RATE_LOCK_WAIT_SECONDS bound.
RATE_FANOUT_LOCKED_TRIALS = 3

# Unlocked trials before the probe is called blind. MEASURED 2026-09-24, 30
# unlocked trials: every one lost calls, 13 or 14 of 16, so three trials is
# already far past what the observed rate needs.
RATE_FANOUT_LOSS_TRIALS = 3

# One writer. It loads the hook FROM ITS REAL PATH, so `__file__` and every root
# the module derives from it are the production ones, then calls the counter the
# way `main()` calls it. `main()` itself is not used: it runs the whole wall
# stack, and in a yard the write guard refuses a `Write` payload before the
# counter is ever reached, so the case would measure a different guard.
#
# The barrier is what makes the loss reproducible rather than lucky. Each child
# announces itself and then spins until the parent releases all sixteen at once,
# so the read-modify-write windows overlap maximally, which is the condition
# the comment in `_dispatch.py` names as the one a runaway loop produces.
RATE_COUNTER_CHILD = """
import contextlib, importlib.util, os, sys, time
root, hook, unlocked, ready, barrier = sys.argv[1:6]
sys.path.insert(0, root)
spec = importlib.util.spec_from_file_location("hos119_rate_counter", hook)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
if unlocked == "1":
    module._rate_state_lock = contextlib.nullcontext
open(os.path.join(ready, str(os.getpid())), "w").close()
while not os.path.exists(barrier):
    time.sleep(0.001)
module.check_rate_limit({"tool_name": "Write",
                         "tool_input": {"file_path": "/invented/%d.txt" % os.getpid()}})
"""


def _rate_fanout(tmp_path: Path, child: Path, unlocked: bool, trial: int) -> tuple:
    """Release RATE_FANOUT_WRITERS writers at once; return (count, degraded).

    `count` is read from the state file the children shared, which is the
    OBSERVABLE consequence: the number the daily cap and the loop detector are
    both computed from. `degraded` counts the children whose lock wait expired
    and which therefore counted unlocked by design; a trial with any of those is
    not evidence about the lock either way.
    """
    run = tmp_path / f"rate-fanout-{int(unlocked)}-{trial}"
    ready = run / "ready"
    ready.mkdir(parents=True)
    state = run / "dispatch-rate.json"
    barrier = run / "go"
    env = dict(os.environ, WS_RATE_LIMIT_STATE=str(state))
    argv = [sys.executable, str(child), str(ROOT),
            str(ROOT / ".claude" / "hooks" / "_dispatch.py"),
            "1" if unlocked else "0", str(ready), str(barrier)]
    procs = [subprocess.Popen(argv, env=env, stderr=subprocess.PIPE, text=True)
             for _ in range(RATE_FANOUT_WRITERS)]
    deadline = time.monotonic() + 60
    while (len(list(ready.iterdir())) < RATE_FANOUT_WRITERS
           and time.monotonic() < deadline):
        time.sleep(0.005)
    barrier.write_text("", encoding="utf-8")
    errs = [p.communicate()[1] or "" for p in procs]
    broken = [f"exit {p.returncode}: {e.strip()[:200]}"
              for p, e in zip(procs, errs, strict=True) if p.returncode != 0]
    assert not broken, f"a writer died, so the count below measures nothing: {broken}"
    assert state.is_file(), f"no writer wrote {state}"
    count = json.loads(state.read_text(encoding="utf-8")).get("count")
    return count, sum("busy for" in e for e in errs)


def test_the_runaway_write_counter_counts_every_call_in_a_fan_out(tmp_path):
    """Sixteen concurrent writers leave sixteen in `count`, and the probe can see loss.

    WHAT WAS MEASURED BEFORE THE LOCK, from `_rate_state_lock`'s own docstring:
    2026-09-04, sixteen hook processes against one state file, fifteen trials,
    240 calls, 70 of them leaving no trace at all, and `count` equal to the
    number of survivors in every trial. The lock closed it and nothing has
    guarded it since the 2026-09-21 test-suite split parked the harness.

    BOTH DIRECTIONS. The first half drives the counter as it ships and requires
    an exact count. The second half drives the SAME code with
    `_rate_state_lock` replaced by a null context, which is the previous version
    reconstructed at the seam rather than by editing the file, and requires the
    loss to be visible. A probe that cannot see loss would pass the first half
    over any implementation at all.

    HOW DETERMINISTIC. Not certain, and it is a race, so it cannot be. MEASURED
    2026-09-24 on this machine, with the barrier below releasing all sixteen at
    once: 20 locked trials counted 16 of 16 every time, and 30 unlocked trials
    lost calls every time, 13 or 14 of 16. Three unlocked trials here against an
    observed 30/30 loss rate; the first losing trial ends the loop.

    THE FAIL-OPEN IS NOT RELITIGATED. `RATE_LOCK_WAIT_SECONDS` stays 2.0 and the
    unlocked-on-expiry path stays. A loaded machine can reach that bound, and
    the answer here is to discard the trial and say so, never to widen the
    timeout: `degraded` counts the children that printed `file_lock`'s own
    stderr line, and a trial holding any of them is not evidence.

    NOT CLAIMED. Nothing here exercises `main()`, the soft or hard cap, the
    loop detector, `check_tool_budget`, or the coverage gaps both checks carry
    (`Bash` is invisible to one; `WebFetch`, `Task` and the MCP tools to the
    other). Nothing asserts that the lock is taken, which would be the guard's own
    condition restated; only that the count survives the contention.
    """
    child = tmp_path / "rate_counter_child.py"
    child.write_text(RATE_COUNTER_CHILD, encoding="utf-8")

    locked = []
    for trial in range(RATE_FANOUT_LOCKED_TRIALS):
        locked.append(_rate_fanout(tmp_path, child, False, trial))
        if locked[-1][1] == 0:
            break
    honest = [count for count, degraded in locked if degraded == 0]
    if not honest:
        pytest.skip(
            f"every one of {RATE_FANOUT_LOCKED_TRIALS} trials hit the lock's own "
            f"bounded-wait fail-open, so this machine was too loaded to say "
            f"anything about the lock either way: {locked}")
    assert honest[-1] == RATE_FANOUT_WRITERS, (
        f"{RATE_FANOUT_WRITERS} writers ran and the counter says {honest[-1]}, so "
        f"{RATE_FANOUT_WRITERS - honest[-1]} tool calls left no trace. The daily "
        f"cap and the loop detector are both computed from this number, and it "
        f"undercounts hardest exactly when the calls overlap. Trials: {locked}")

    lost = []
    for trial in range(RATE_FANOUT_LOSS_TRIALS):
        count, _ = _rate_fanout(tmp_path, child, True, trial)
        lost.append(RATE_FANOUT_WRITERS - count)
        if lost[-1] > 0:
            break
    assert max(lost) > 0, (
        f"with the lock removed, {RATE_FANOUT_LOSS_TRIALS} fan-outs of "
        f"{RATE_FANOUT_WRITERS} still lost nothing ({lost}). The probe above is "
        f"therefore blind and its exact count proves nothing. MEASURED "
        f"2026-09-24: 30 unlocked trials lost 13-14 of 16, every time.")


# ============================================================
# THE NINTH: an agent in a yard's pane carries the yard marker
# ============================================================
#
# Added 2026-09-27. MEASURED that day: a yard's agent was started with
# `herdr agent start` while the bootstrap was at step 9, ran WITHOUT
# `HEADING_OS_YARD`, so every yard-side guard keyed on the marker was inert in
# it, and the bootstrap's launch line then reached it as a prompt. Two halves
# hold it: the PreToolUse wall refuses `herdr agent start` into a yard's pane,
# and the bootstrap never types its launch line into an occupied pane. The
# bootstrap is driven for real, against a FAKE checkout whose `.venv/bin/python`
# and `uv` are stubs, so every step runs in well under a second and nothing
# reaches herdr, uv or pytest. The herdr stub is local because it has to ANSWER
# `pane get`.

AGENT_START_REFUSED_FLOOR = 6    # MEASURED 2026-09-27: the six refusals below
AGENT_START_ALLOWED_FLOOR = 4    # and the four that must pass


def test_herdr_agent_start_into_a_yard_pane_is_refused_and_helm_still_passes(
        dispatch, monkeypatch, tmp_path):
    """`herdr agent start` into a linked worktree's pane, or a pane nobody can
    resolve, is refused; the same command into the main clone's pane is not."""
    module, _helm, _overlay, neutral = dispatch
    yard = tmp_path / "a-yard"
    yard.mkdir()
    (yard / ".git").write_text("gitdir: /nowhere/worktrees/a-yard\n", encoding="utf-8")
    main = tmp_path / "a-main-clone"
    (main / ".git").mkdir(parents=True)
    session = tmp_path / "session.json"
    session.write_text(json.dumps({"workspaces": [
        {"id": "wY1", "worktree_space": {"checkout_path": str(yard)}},
        {"id": "wM1", "worktree_space": {"checkout_path": str(main)}},
        {"id": "wG1", "worktree_space": {"checkout_path": str(tmp_path / "gone")}},
    ]}), encoding="utf-8")
    monkeypatch.setattr(module, "_yard_herdr_session_files", lambda: [session])

    refused = [
        "herdr agent start a --kind claude --pane wY1:p1",
        "herdr agent start a --kind claude --pane=wY1:p1 --timeout 5000",
        f"cd {tmp_path} && HERDR_ENV=1 herdr agent start a --kind claude --pane 'wY1:p1'",
        "herdr agent start a --kind claude --pane wZZ:p1",
        "herdr agent start a --kind claude --pane wG1:p1",
        "herdr agent start a --kind claude",
    ]
    allowed = [
        "herdr agent start a --kind claude --pane wM1:p1",
        "herdr agent start --help",
        'herdr pane run wY1:p1 "HEADING_OS_YARD=1 claude --dangerously-skip-permissions"',
        'herdr agent prompt wY1:p1 "do not run herdr agent start --pane wY1:p1"',
    ]
    assert len(refused) >= AGENT_START_REFUSED_FLOOR
    assert len(allowed) >= AGENT_START_ALLOWED_FLOOR

    def decide(command):
        return module.check_yard_agent_start_guard(
            {"tool_name": "Bash", "cwd": str(neutral), "tool_input": {"command": command}})

    passed = [c for c in refused if decide(c) is None]
    assert not passed, f"the agent-start wall let these through: {passed}"
    blocked = [c for c in allowed if decide(c) is not None]
    assert not blocked, f"the agent-start wall refused honest commands: {blocked}"
    reason = decide(refused[0])["reason"]
    assert 'herdr pane run wY1:p1 "HEADING_OS_YARD=1 claude' in reason
    assert module.check_yard_agent_start_guard in module.CHECKS


FAKE_PYTHON = """#!/usr/bin/env bash
# `.venv/bin/python` of a fake checkout. pytest is the tree-clean wall: green
# until the canary decoy exists, red once it does. The probe chooser prints
# the first candidate. Everything else succeeds.
case "$*" in
  *"-m pytest"*) [ -e admin/.yard-canary-probe.md ] && exit 1; exit 0 ;;
  *routing_verdict*) echo admin/.yard-canary-probe.md ;;
esac
exit 0
"""

ANSWERING_HERDR = """
import pathlib, sys
here = pathlib.Path(__file__).parent
with (here / "calls.log").open("a", encoding="utf-8") as handle:
    handle.write("\\x1f".join(sys.argv[1:]) + "\\n")
if sys.argv[1:3] == ["pane", "get"]:
    sys.stdout.write((here / "pane-get.json").read_text(encoding="utf-8"))
"""


def _fake_checkout(tmp_path: Path, pane_agent: str | None) -> tuple[Path, dict, Path]:
    """A checkout the real bootstrap can run end to end in under a second."""
    fake = tmp_path / "fake-yard"
    (fake / "scripts" / "utils").mkdir(parents=True)
    (fake / "scripts" / "utils" / "paths.py").write_text("", encoding="utf-8")
    (fake / "pyproject.toml").write_text("", encoding="utf-8")
    tools = tmp_path / "tools"
    tools.mkdir()
    for path, body in (
            (fake / "scripts" / "setup-platform.sh", "#!/bin/sh\nexit 0\n"),
            (fake / ".venv" / "bin" / "python", FAKE_PYTHON),
            (tools / "uv", "#!/bin/sh\nexit 0\n"),
            (tools / "herdr", f"#!{sys.executable}\n{ANSWERING_HERDR}")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    _git(fake, "init", "-q", "-b", "yard-zzqx-branch")
    pane = {"pane_id": "wZ9:p1", "agent": pane_agent} if pane_agent else {"pane_id": "wZ9:p1"}
    (tools / "pane-get.json").write_text(
        json.dumps({"id": "x", "result": {"pane": pane, "type": "pane"}}), encoding="utf-8")
    data = tmp_path / "data-overlay"
    data.mkdir()
    env = {k: v for k, v in os.environ.items()
           if k not in ("HERDR_WORKSPACE_ID", "HERDR_PANE_ID", "HEADING_OS_YARD",
                        "HERDR_PLUGIN_CONTEXT_JSON", "FORCE_BOOTSTRAP")}
    env.update({
        "PATH": f"{tools}:{env.get('PATH', '')}",
        "HERDR_BIN_PATH": str(tools / "herdr"),
        "HEADING_OS_DATA": str(data),
        "HEADING_OS_AGENT_CMD": "claude --zzqx",
        "HERDR_PLUGIN_EVENT_JSON": json.dumps({"data": {
            "worktree": {"path": str(fake), "branch": "yard-zzqx-branch"},
            "workspace": {"workspace_id": "wZ9", "root_pane": {"pane_id": "wZ9:p1"}}}}),
    })
    return fake, env, tools / "calls.log"


def _bootstrap(fake: Path, env: dict) -> subprocess.CompletedProcess:
    script = ROOT / "scripts" / "herdr" / "heading-os-yard" / "yard-bootstrap.sh"
    return subprocess.run(["bash", str(script)], cwd=str(fake), env=env,
                          capture_output=True, text=True, timeout=120)


def _calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [line.split("\x1f") for line in log.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("occupant", ["none", "pane-agent", "claude-process"])
def test_the_bootstrap_never_types_its_launch_line_into_an_occupied_pane(
        tmp_path, occupant):
    """An agent already in the pane, reported by herdr or found in /proc with
    its cwd in the checkout, holds the launch line back; a free pane gets it."""
    fake, env, log = _fake_checkout(
        tmp_path, "claude" if occupant == "pane-agent" else None)
    squatter = None
    if occupant == "claude-process":
        # A process whose argv[0] is `claude`, standing in the checkout. A copy
        # of `sleep`, because this repository uses no symlinks.
        binary = tmp_path / "bin" / "claude"
        binary.parent.mkdir()
        binary.write_bytes(Path("/bin/sleep").read_bytes())
        binary.chmod(0o755)
        squatter = subprocess.Popen(["claude", "30"], executable=str(binary), cwd=str(fake))
    try:
        result = _bootstrap(fake, env)
    finally:
        if squatter is not None:
            squatter.kill()
            squatter.wait()
    assert result.returncode == 0, result.stderr
    sent = [c for c in _calls(log) if c[:2] == ["pane", "run"]]
    if occupant == "none":
        assert len(sent) == 1 and "export HEADING_OS_YARD=1; claude --zzqx" in sent[0][-1]
    else:
        assert not sent, f"the launch line was typed into an occupied pane: {sent}"
        assert 'herdr pane run wZ9:p1 "HEADING_OS_YARD=1 claude --zzqx"' in result.stderr


@pytest.mark.parametrize("admin_existed", [False, True])
def test_the_canary_removes_the_directories_it_made_and_no_other(tmp_path, admin_existed):
    """`mkdir -p` for the decoy made `admin/`; cleanup takes it back only then."""
    fake, env, _log = _fake_checkout(tmp_path, None)
    if admin_existed:
        (fake / "admin").mkdir()
    result = _bootstrap(fake, env)
    assert result.returncode == 0, result.stderr
    assert "canary passed" in result.stderr
    assert not (fake / "admin" / ".yard-canary-probe.md").exists()
    assert (fake / "admin").is_dir() is admin_existed


def test_a_reopened_yard_gets_its_label_and_no_agent(tmp_path):
    """The `status: ok` short-circuit still labels the workspace, and starts nothing."""
    fake, env, log = _fake_checkout(tmp_path, None)
    (fake / ".claude").mkdir()
    (fake / ".claude" / ".yard-bootstrap-status").write_text(
        '{"status":"ok","step":11}\n', encoding="utf-8")
    result = _bootstrap(fake, env)
    assert result.returncode == 0, result.stderr
    assert "bootstrap already complete" in result.stderr
    calls = _calls(log)
    assert ["workspace", "rename", "wZ9", "wZ9 · zzqx-branch"] in calls
    assert not [c for c in calls if c[:2] == ["pane", "run"]]


# ============================================================
# The tenth: a checkout is offered only its own handoff (HOS-158)
# ============================================================

HANDOFF_TEXT = "# Handoff\n\n## Objective\n\n{marker}\n\n## Next steps\n\n- {marker} step\n"


def test_a_checkout_is_offered_its_own_handoff_and_never_another_checkouts(tmp_path):
    """/prime and /next in one checkout never present a handoff written from another.

    FAILURE B: a yard acting on state that is not its own. MEASURED 2026-09-28
    in HELM (HOS-158): /prime in a fresh yard read the one machine-wide pointer
    `.latest/summary.md`, found HELM's last handoff and asked whether to
    continue it. Both readers are driven through their real entry points from
    two copied checkouts sharing one throwaway data root: `checkpoint-paths.py
    --json`, whose summary pointers are what /prime opens, and `next-signal.py
    --json`, which /next renders. Every key ending `summary_pointer` is read,
    whatever it is called, so the case does not depend on the new key's name.
    """
    data = tmp_path / "data"
    data.mkdir()
    helm, yard = tmp_path / "zzqx-helm", tmp_path / "zzqx-yard"
    # `zzqx_leak_probe_*` is the transient file tests/test_gate_leak_guard.py
    # plants in the live scripts/ and unlinks within one gate run; it must stay
    # in the live tree to drive the real gate. Under xdist it can vanish between
    # copytree's listing and its copy, which raised shutil.Error. It is not part
    # of either copied checkout, so it is left out of the copy.
    for checkout in (helm, yard):
        for part in ("scripts", "config"):
            shutil.copytree(ROOT / part, checkout / part, ignore=shutil.ignore_patterns("__pycache__", "zzqx_leak_probe_*"))
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PROJECT_DIR", "WORKSPACE_ROOT")}
    env["HEADING_OS_DATA"] = str(data)

    def run(checkout, sid, script, *args, stdin=None):
        result = subprocess.run(
            [sys.executable, f"scripts/{script}", *args], cwd=checkout, input=stdin,
            env={**env, "CLAUDE_CODE_SESSION_ID": sid},
            capture_output=True, text=True, timeout=300)
        assert result.returncode == 0, result.stderr
        return result.stdout

    def offered(checkout, sid):
        paths = json.loads(run(checkout, sid, "checkpoint-paths.py", "--json"))
        prime = "".join(
            (Path(paths["data_root"]) / ref).read_text(encoding="utf-8")
            for key, ref in paths.items() if key.endswith("summary_pointer")
            and (Path(paths["data_root"]) / ref).is_file())
        handoff = json.loads(run(checkout, sid, "next-signal.py", "--json"))["handoff"]
        return prime, (handoff or {}).get("objective", "")

    def write(checkout, sid, marker):
        run(checkout, sid, "checkpoint-paths.py", "--write",
            stdin=HANDOFF_TEXT.format(marker=marker))

    write(helm, "zzqx0001-helm", "ZZQX-HELM-WORK")
    prime, objective = offered(yard, "zzqx0002-yard")
    assert "ZZQX-HELM-WORK" not in prime, "/prime in the yard is shown HELM's handoff"
    assert objective != "ZZQX-HELM-WORK", "/next in the yard is shown HELM's handoff"

    # The honest half: each checkout still finds its own, after the other wrote.
    write(yard, "zzqx0002-yard", "ZZQX-YARD-WORK")
    prime, objective = offered(yard, "zzqx0003-yard")
    assert "ZZQX-YARD-WORK" in prime and objective == "ZZQX-YARD-WORK"
    prime, objective = offered(helm, "zzqx0004-helm")
    assert "ZZQX-HELM-WORK" in prime and objective == "ZZQX-HELM-WORK"
    assert "ZZQX-YARD-WORK" not in prime


ZZQX_REGISTRY = """version: 1
names:
  checkout-state:
    what: "one checkout's hook and guard state"
    runtime: hook-state/tmp
    under: "{prefix}/{{checkout}}"
services:
  hook-state: "the guards' per-checkout state"
"""


def test_a_guard_is_never_answered_from_a_registry_the_disk_cache_has_outlived(tmp_path):
    """A registry edit reaches the next process, however the edit is disguised.

    FAILURE B: a guard acting on an address that is no longer this checkout's.
    HOS-173 (2026-09-28) put the parsed registry on disk so a fresh hook skips
    PyYAML and the parse; every guard's state directory is read through it
    (`checkout_state_root`). MEASURED 2026-09-28 on the pre-change engine: no
    cache entry is ever written, so the first assertion is red there. The
    refusing half edits the registry the way `cp -p` or a snapshot rollback
    would, same length and the old mtime put back, and the next process must
    still answer the new address. The honest half: an unchanged registry is
    answered from the entry, and a corrupt entry is said on stderr and parsed
    past rather than believed.
    """
    data = tmp_path / "data"
    registry = data / "config" / "data-paths.yaml"
    registry.parent.mkdir(parents=True)
    registry.write_text(ZZQX_REGISTRY.format(prefix="aa"), encoding="utf-8")
    cache = data / "runtime-junk" / "data-path" / "cache"
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PROJECT_DIR", "WORKSPACE_ROOT", "HEADING_OS_DATA_READONLY")}
    env["HEADING_OS_DATA"] = str(data)

    def resolve():
        result = subprocess.run(
            [sys.executable, "scripts/data-path.py", "path", "checkout-state", "checkout=zzqx"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr
        return Path(result.stdout.strip()).relative_to(data.resolve()), result.stderr

    first, _ = resolve()
    assert first == Path("runtime-junk/hook-state/tmp/aa/zzqx")
    entries = sorted(cache.glob("registry-*.json"))
    # One entry for this registry and one for the engine default it read the
    # store name from; the pre-change engine writes neither.
    assert len(entries) == 2, f"the parse was not cached: {entries}"

    # Honest half: unchanged text, answered from the entry, nothing new written.
    assert resolve()[0] == first
    assert sorted(cache.glob("registry-*.json")) == entries

    # Refusing half: same length, old mtime restored. Never the old address.
    before = registry.stat()
    registry.write_text(ZZQX_REGISTRY.format(prefix="bb"), encoding="utf-8")
    os.utime(registry, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert registry.stat().st_size == before.st_size
    assert resolve()[0] == Path("runtime-junk/hook-state/tmp/bb/zzqx")

    # An entry holding another text's document is said, parsed past, and
    # rewritten: here the OLD registry's, well-formed, under the new name.
    current = sorted(set(cache.glob("registry-*.json")) - set(entries))
    assert len(current) == 1
    stale = next(e for e in entries if "hook-state" in e.read_text(encoding="utf-8"))
    current[0].write_text(stale.read_text(encoding="utf-8"), encoding="utf-8")
    answer, stderr = resolve()
    assert answer == Path("runtime-junk/hook-state/tmp/bb/zzqx")
    assert "corrupt" in stderr
    assert json.loads(current[0].read_text(encoding="utf-8"))["key"] == current[0].stem
