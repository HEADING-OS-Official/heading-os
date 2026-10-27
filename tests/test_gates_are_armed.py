"""GUARANTEE: every gate this repository relies on is registered, scoped, and actually installed in this checkout.

FAILURE A AND FAILURE B, because the gates being asked about here are the ones
that hold both: the commit-time content scans that keep a private byte out of the
public tree, and the session hooks behind the walls that keep a worktree off
anything live. A gate nothing invokes refuses nothing whatever its code says, and
its green line in the output reads exactly like a gate that ran.

WHAT `scripts/check-gate-integrity.py` ALREADY OWNS, so that this file extends it
rather than duplicating it. That script reads `.pre-commit-config.yaml` and
NOTHING ELSE, and only the `repo: local` hooks inside it. It asks two questions:
does a hook's `files:` regex match at least one tracked path, and is the script
the hook invokes named as a literal string anywhere under `tests/`. It runs on
every commit and in CI.

WHAT THIS FILE ADDS, none of which that script can see:

  * The FOUR REMOTE repositories in the same config, which it skips entirely
    because it filters on `repo == "local"`. Their hooks are the ones carrying
    the standalone scanners, and one of them declares a `files:` pattern that has
    never been checked against the tree by anything.
  * Whether the commit gate is INSTALLED here at all. The config is committed, so
    it is present in every clone, including one where `pre-commit install` was
    never run. `scripts/install-hooks.py --check` asks the hook file git would
    actually run.
  * Whether `core.hooksPath` redirects git away from that file. Setting it is one
    of the two gates an author can disarm by hand, per `.claude/rules/security.md`.
  * Whether the SESSION hooks are registered, through
    `scripts/setup-platform.sh --check`. MEASURED 2026-09-20 and recorded in that
    script's own header: a clone where it has never run arms 2 hooks of 19, and
    the absent ones include the dispatcher behind eleven PreToolUse walls.

NOT CLAIMED. Nothing here runs a gate's checks or observes one refusing; that is
what the per-gate files in this directory are for. A hook that is registered,
scoped, installed and broken passes every case below. Neither does this read the
CI workflow: a guard step with no pre-commit sibling is outside both this file
and the script it extends, and that gap is named rather than left to be found.
The `setup-platform.sh --check` case is RED, by design, on a clone where setup
has never been run; the fix is to run setup, not to soften the case.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.utils.repo_files import IndexUnreadable, git_index_paths  # noqa: E402

# Every floor is asserted OUTSIDE its loop, because a loop over a config that
# failed to parse into hooks passes while checking nothing. A floor that moves
# DOWN costs coverage and is never edited to make a run green.
# Local: 29, MEASURED 2026-09-21 (from 27: three commit-gate hooks in, one out).
# Remote: 8 hooks from 4 repositories, MEASURED 2026-09-21.
# Scoped: 12, MEASURED 2026-09-28 (14 -> 13 sentinel-integration-tests removed
# 2026-09-21; 13 -> 12 lock-exits removed with the mover, HOS-166).
# Tracked settings: 2 registrations, the two `.claude/settings.json` owns.
LOCAL_HOOKS_FLOOR = 29
REMOTE_HOOKS_FLOOR = 8
SCOPED_HOOKS_FLOOR = 11  # the official tree does not ship verify-skills-lock
TRACKED_SETTINGS_REGISTRATIONS_FLOOR = 2


def _config() -> dict:
    path = ROOT / ".pre-commit-config.yaml"
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        pytest.fail(f"{path} could not be read, so no gate registration can be "
                    f"confirmed: {exc}")
    if not isinstance(config, dict) or not isinstance(config.get("repos"), list):
        pytest.fail(f"{path} holds no repos list; the commit gate's declaration "
                    f"has changed shape and every case below is now blind")
    return config


def _hooks(config: dict) -> tuple[list[dict], list[dict]]:
    """Every hook the config declares, split local from remote. Asked of the
    config at run time; no hook id is copied into this file."""
    local, remote = [], []
    for repo in config["repos"]:
        bucket = local if repo.get("repo") == "local" else remote
        bucket.extend(h for h in repo.get("hooks", []) if isinstance(h, dict) and h.get("id"))
    return local, remote


def _tracked() -> list[str]:
    try:
        return git_index_paths(ROOT)
    except IndexUnreadable as exc:
        pytest.fail(f"git's index could not be listed, so no `files:` pattern "
                    f"can be checked against the tree: {exc}")


def _run(args: list[str], **kw) -> subprocess.CompletedProcess:
    """The REAL entry point, not a helper imported from it."""
    return subprocess.run(args, cwd=str(ROOT), capture_output=True, text=True,
                          timeout=300, **kw)


def test_every_declared_hook_local_and_remote_has_a_scope_that_matches_the_tree():
    """A `files:` pattern matching nothing passes every commit, vacuously, forever.

    The remote half is the extension: `check-gate-integrity.py` filters on
    `repo == "local"` and has never asked this of the four remote repositories.
    """
    local, remote = _hooks(_config())
    assert len(local) >= LOCAL_HOOKS_FLOOR, (
        f"{len(local)} local hooks, below the {LOCAL_HOOKS_FLOOR} measured "
        f"2026-09-21; hooks were removed from the commit gate")
    assert len(remote) >= REMOTE_HOOKS_FLOOR, (
        f"{len(remote)} remote hooks, below the {REMOTE_HOOKS_FLOOR} measured "
        f"2026-09-21")

    tracked = _tracked()
    scoped, vacuous = 0, []
    for hook in local + remote:
        pattern = hook.get("files")
        if not pattern or hook.get("always_run"):
            continue
        scoped += 1
        try:
            rx = re.compile(pattern)
        except re.error:
            vacuous.append(f"{hook['id']}: `files:` does not compile")
            continue
        if not any(rx.search(path) for path in tracked):
            vacuous.append(f"{hook['id']}: `files:` matches no tracked path")
    assert scoped >= SCOPED_HOOKS_FLOOR, (
        f"{scoped} hooks declare a `files:` pattern, below the "
        f"{SCOPED_HOOKS_FLOOR} measured 2026-09-21; the loop above checked "
        f"fewer gates than the config carries")
    assert not vacuous, "gate(s) scoped to nothing: " + "; ".join(vacuous)


def test_the_tracked_settings_file_still_registers_its_session_hooks():
    """The two registrations the tracked file owns, read from the file itself.

    `ARCHITECTURE.md` section 7, now under `spec-docs` in the data repository,
    records which two and why; this asserts only
    that they are present and each carries a command to run, because a
    registration with an empty command is a hook that fires and does nothing.
    """
    path = ROOT / ".claude" / "settings.json"
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        pytest.fail(f"{path} could not be read, so no session hook "
                    f"registration can be confirmed: {exc}")
    registrations = [hook
                     for matchers in (settings.get("hooks") or {}).values()
                     for matcher in matchers
                     for hook in matcher.get("hooks", [])]
    assert len(registrations) >= TRACKED_SETTINGS_REGISTRATIONS_FLOOR, (
        f"{len(registrations)} hook registrations in {path}, below the "
        f"{TRACKED_SETTINGS_REGISTRATIONS_FLOOR} measured 2026-09-21")
    assert all(hook.get("command") for hook in registrations), (
        f"a registration in {path} carries no command, so it fires and runs "
        f"nothing")


def test_the_commit_gate_is_installed_here_and_git_is_not_redirected_away_from_it():
    """Both of the two gates an author can disarm by hand, asserted together.

    The exit code is the assertion, never the message. `core.hooksPath` is asked
    of git rather than of a config file, because git resolves it from several and
    a literal path value once silently bypassed every hook.
    """
    armed = _run([sys.executable, str(ROOT / "scripts" / "install-hooks.py"), "--check"])
    assert armed.returncode == 0, (
        "no commit gate is armed in this checkout; run `pre-commit install`:\n"
        f"{armed.stdout}\n{armed.stderr}")
    redirect = _run(["git", "config", "--get", "core.hooksPath"])
    assert redirect.returncode != 0, (
        f"core.hooksPath is set to {redirect.stdout.strip()!r}, which redirects "
        f"git away from the installed hook and disarms every commit gate")


def test_the_arming_check_refuses_a_clone_whose_hook_is_absent_or_not_the_frameworks(tmp_path):
    """The refusal direction, and without it `exit 0` would leave this file green.

    Three states of one clone, each driving the real command: no hook file, a
    hook file that is somebody else's, and the framework's own. The marker is
    imported from the checker rather than written here, so a change to what the
    framework stamps is followed rather than drifted from.
    """
    spec = importlib.util.spec_from_file_location(
        "igh", ROOT / "scripts" / "install-git-hooks.py")
    if spec is None or spec.loader is None:
        pytest.fail("scripts/install-git-hooks.py could not be loaded, so the "
                    "marker the gate looks for cannot be read from the gate")
    igh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(igh)
    marker = getattr(igh, "PRE_COMMIT_FRAMEWORK_MARKER", "")
    assert marker, "the checker defines no framework marker; this case is blind"

    clone = tmp_path / "zzqx-clone"
    (clone / ".git" / "hooks").mkdir(parents=True)
    (clone / ".claude").mkdir()
    (clone / "CLAUDE.md").write_text("ZZQX\n", encoding="utf-8")
    (clone / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    hook = clone / ".git" / "hooks" / "pre-commit"
    check = [sys.executable, str(ROOT / "scripts" / "install-hooks.py"), "--check"]
    env = {"WORKSPACE_ROOT": str(clone), "PATH": "/usr/bin:/bin"}

    assert _run(check, env=env).returncode != 0, (
        "a clone with the config and no installed hook was reported as armed")

    hook.write_text("#!/bin/sh\n# ZZQX somebody else's hook\nexit 0\n", encoding="utf-8")
    hook.chmod(0o755)
    assert _run(check, env=env).returncode != 0, (
        "a hook file carrying no framework marker was reported as armed")

    hook.write_text(f"#!/bin/sh\n# {marker}\nexit 0\n", encoding="utf-8")
    hook.chmod(0o755)
    assert _run(check, env=env).returncode == 0, (
        "the framework's own hook was reported as NOT armed, so this check "
        "refuses everything and proves nothing above")


def test_the_session_hooks_are_registered_and_the_check_refuses_a_clone_where_they_are_not(tmp_path):
    """Both directions of `setup-platform.sh --check`, which writes nothing.

    The refusal case is a scratch clone holding the script and its templates and
    no live settings file, which is the state of every clone that skipped setup.
    """
    here = _run(["bash", str(ROOT / "scripts" / "setup-platform.sh"), "--check"])
    assert here.returncode == 0, (
        "this checkout does not register every session hook its template "
        "defines; run `bash scripts/setup-platform.sh`:\n"
        f"{here.stdout}\n{here.stderr}")

    clone = tmp_path / "zzqx-unarmed"
    (clone / "scripts").mkdir(parents=True)
    (clone / ".claude").mkdir()
    shutil.copy(ROOT / "scripts" / "setup-platform.sh", clone / "scripts")
    templates = sorted((ROOT / ".claude").glob("settings.local.*.json"))
    assert templates, ("no settings.local.*.json template exists, so the "
                       "refusal case below has nothing to check against")
    for template in templates:
        shutil.copy(template, clone / ".claude")
    (clone / ".claude" / "settings.local.json").unlink(missing_ok=True)

    unarmed = _run(["bash", str(clone / "scripts" / "setup-platform.sh"), "--check"])
    assert unarmed.returncode != 0, (
        "a clone with no settings.local.json was reported as armed:\n"
        f"{unarmed.stdout}\n{unarmed.stderr}")
    assert not (clone / ".claude" / "settings.local.json").exists(), (
        "--check wrote a settings file; it is a diagnostic and must write "
        "nothing")


# The tool names the Hindsight wall must hear, in the two shapes the two MCP
# routes on the operator's machine produce: a bridge server whose tools carry no
# prefix, and the plugin's server, whose tools carry `hindsight_`. The server
# names here are INVENTED (`zzqx-...`) on purpose, except `hindsight`, which is
# the plugin's own published name: the wall and the matcher are meant to work
# by pattern, so a renamed server is the case worth proving.
_HINDSIGHT_QUERY_TOOLS = ("mcp__zzqx-hindsight__recall",
                          "mcp__hindsight__hindsight_search_knowledge_pages")
_HINDSIGHT_WRITE_TOOLS = ("mcp__zzqx-hindsight__retain",
                          "mcp__hindsight__hindsight_ingest_document")


def _dispatch_denies(payload: dict, env: dict) -> bool:
    """Run the REAL PreToolUse entry point once; True when it refused the call.

    A refusal from this wall renders as a `permissionDecision: deny` on stdout
    with exit 0, the `_policy_deny` path; anything else is an allow.
    """
    result = subprocess.run(
        [sys.executable, str(ROOT / ".claude" / "hooks" / "_dispatch.py")],
        input=json.dumps(payload), cwd=payload["cwd"], env=env,
        capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, (
        f"the dispatcher exited {result.returncode}:\n{result.stderr}")
    try:
        out = json.loads(result.stdout) if result.stdout.strip() else {}
    except ValueError:
        pytest.fail(f"the dispatcher printed something that is not JSON: "
                    f"{result.stdout!r}")
    return (out.get("hookSpecificOutput") or {}).get(
        "permissionDecision") == "deny" or out.get("decision") == "block"


def test_the_memory_wall_opens_only_on_a_hindsight_query_and_never_wedges(tmp_path):
    """`check_hindsight_first`, driven through `_dispatch.py`'s own stdin.

    The wall refuses a session's first lookup into the records corpus until
    Hindsight has been ASKED. MEASURED 2026-10-04 against the previous wall
    (`check_cognee_first`, HEAD of the HOS-198 branch, run from a copy outside
    the checkout): with a Hindsight key file and no Cognee store, the first
    lookup was ALLOWED, so the refusing half below is red on it, and a Hindsight
    MCP query could not have opened it anyway, since that wall never read one.

    Refused, each in a fresh session: a records Read with nothing asked; the
    same Read after `echo hindsight memory recall ...` (a mention, not a run);
    after a Cognee command; after a Hindsight WRITE tool. Allowed: the same Read
    after either MCP query shape, after a CLI query whose server is unreachable
    (the stamp lands before the call runs, so an erroring attempt still
    unlocks), and on a machine with no Hindsight configured at all.
    """
    neutral = tmp_path / "zzqx-cwd"
    neutral.mkdir()
    key = tmp_path / "zzqx-hindsight-key"
    key.write_text("zzqx-not-a-key\n", encoding="utf-8")
    env = dict(os.environ, HEADING_OS_STATE_DIR=str(tmp_path / "state"),
               HINDSIGHT_INJECT_KEY_FILE=str(key))
    lookup = ("Read", {"file_path": str(tmp_path / "auto-memory" / "zzqx.md")})

    def deny(session: str, tool: str, tool_input: dict, run_env=env) -> bool:
        return _dispatch_denies({"session_id": session, "tool_name": tool,
                                 "tool_input": tool_input, "cwd": str(neutral),
                                 "hook_event_name": "PreToolUse"}, run_env)

    def lookup_after(session: str, tool: str, tool_input: dict) -> bool:
        assert not deny(session, tool, tool_input), (
            f"the unlocking call itself was refused: {tool} {tool_input}")
        return deny(session, *lookup)

    assert deny("zzqx-s0", *lookup), (
        "the first records lookup of a session went through with no Hindsight "
        "query asked; the wall is not armed")

    keys = [("zzqx-mcp-" + str(i), t, {"query": "zzqx"})
            for i, t in enumerate(_HINDSIGHT_QUERY_TOOLS)]
    keys.append(("zzqx-cli-down", "Bash", {"command":
                 "HINDSIGHT_API_URL=http://127.0.0.1:9 hindsight -o json "
                 "memory recall zzqx 'what was decided'"}))
    still_shut = [(f"zzqx-w{i}", t, {"content": "zzqx"})
                  for i, t in enumerate(_HINDSIGHT_WRITE_TOOLS)]
    still_shut += [
        ("zzqx-echo", "Bash", {"command": "echo hindsight memory recall zzqx q"}),
        ("zzqx-retain", "Bash", {"command": "hindsight memory retain zzqx q"}),
        ("zzqx-cognee", "Bash",
         {"command": ".venv/bin/python scripts/cognee-pilot.py search q"}),
    ]
    assert len(keys) >= 3 and len(still_shut) >= 5, "the case lists collapsed"

    stuck = [k for k in keys if lookup_after(*k)]
    assert not stuck, f"a Hindsight query did not open the wall: {stuck}"
    opened = [k for k in still_shut if not lookup_after(*k)]
    assert not opened, f"something that is not a Hindsight query opened it: {opened}"

    absent = dict(env, HINDSIGHT_INJECT_KEY_FILE=str(tmp_path / "zzqx-no-key"))
    assert not deny("zzqx-nohs", *lookup, run_env=absent), (
        "a machine with no Hindsight configured was caged behind a query it "
        "cannot make")


def test_every_platform_template_routes_hindsight_queries_to_the_dispatcher():
    """A wall whose unlock never reaches the hook is a cage.

    MEASURED 2026-10-04 before the fix: all three tracked templates registered
    `_dispatch.py` under six PreToolUse matchers, none of which matched a
    Hindsight MCP tool, so the MCP door of the memory wall was unreachable.
    The same day the turn-capture Stop hook left the engine (HOS-198), so no
    template may still register it.
    """
    templates = sorted((ROOT / ".claude").glob("settings.local.*.json"))
    assert len(templates) >= 2, (
        f"{len(templates)} platform templates, below the 2 the official tree ships")
    unrouted, leftover = [], []
    for template in templates:
        hooks = json.loads(template.read_text(encoding="utf-8")).get("hooks") or {}
        matchers = [group.get("matcher") or ""
                    for group in hooks.get("PreToolUse", [])
                    if any("_dispatch.py" in h.get("command", "")
                           for h in group.get("hooks", []))]
        for tool in _HINDSIGHT_QUERY_TOOLS:
            if not any(re.fullmatch(m, tool) for m in matchers):
                unrouted.append(f"{template.name}: {tool}")
        leftover += [f"{template.name}: {event}"
                     for event, groups in hooks.items() for group in groups
                     for h in group.get("hooks", [])
                     if "cognee-capture.py" in h.get("command", "")]
    assert not unrouted, f"Hindsight queries the dispatcher never sees: {unrouted}"
    assert not leftover, f"a removed hook is still registered: {leftover}"
