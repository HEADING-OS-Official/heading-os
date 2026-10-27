#!/usr/bin/env python3
"""Apply pipeline for the update manager: snapshot -> apply -> health-gate ->
auto-rollback. The apply invariant: an auto update never leaves a component
broken -- health passes on the new version, or the old version is restored.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.utils.update_registry import Component  # noqa: E402
from scripts.utils.update_common import classify, resolve_current, versions_differ, write_state  # noqa: E402


class RollbackFailed(Exception):
    """The restore command itself failed, so the prior version is NOT back.

    Distinct from "rolled back": the module docstring's invariant is that an
    auto update never leaves a component broken, and a rollback that exits
    non-zero breaks it. `_build_rollback` ran its command with `check=False` and
    discarded the exit status, so a rollback exiting 7 and restoring nothing
    produced the same "rolled-back" verdict as a rollback that worked. Measured
    2026-08-26 with apply={'cmd': 'true', 'rollback_cmd': 'exit 7'} and a
    failing health gate.
    """


def run_health(comp: Component) -> bool:
    if not comp.health:
        return True
    cmd = comp.health.get("cmd", "")
    try:
        res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True,
                             timeout=60, check=False)
    except (subprocess.SubprocessError, OSError):
        return False
    want = comp.health.get("expect_substr")
    out = res.stdout + res.stderr
    if want:
        return want in out
    return res.returncode == 0


def apply_one(comp: Component, *, applier: Callable[[], None],
              rollback: Callable[[], None]) -> str:
    if comp.tier == "observed" or comp.apply is None:
        return "skipped"
    if comp.hold or comp.pin:
        return "skipped"
    try:
        applier()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        # The apply command failed or timed out (not the health gate). Restore the
        # prior version. For script applies the closure is a no-op -- the script
        # self-rolls-back before exiting non-zero, so the exception already means
        # "restored".
        try:
            rollback()
        except RollbackFailed as exc:
            print(exc, file=sys.stderr)
            return "rollback-failed"
        return "rolled-back"
    # A `script` apply is its own gate: it self-verified health and self-rolled-back
    # before exiting non-zero, so reaching here (exit 0) means healthy. Re-probing
    # can only produce a false negative — its rollback closure is a no-op with no
    # teeth. Only `cmd` applies need the outer health gate (their sole gate, with a
    # real rollback_cmd).
    if comp.apply and "cmd" not in comp.apply and "script" in comp.apply:
        return "applied"
    if run_health(comp):
        return "applied"
    try:
        rollback()
    except RollbackFailed as exc:
        print(exc, file=sys.stderr)
        return "rollback-failed"
    return "rolled-back"


def _default_applier(comp: Component) -> Callable[[], None]:
    """Build the applier for a component: a `cmd` one-liner, or a `script`."""
    def _run() -> None:
        if "cmd" in comp.apply:
            subprocess.run(["bash", "-c", comp.apply["cmd"]], check=True, timeout=600)
        elif "script" in comp.apply:
            root = Path(__file__).resolve().parent.parent.parent
            subprocess.run([sys.executable, str(root / comp.apply["script"])],
                          check=True, timeout=900)
        else:
            raise ValueError(f"{comp.name}: apply block has neither cmd nor script")
    return _run


def _build_rollback(comp: Component, prev: str) -> Callable[[], None]:
    """Rollback closure. For a `cmd` apply, run `rollback_cmd` with {prev}
    substituted. A `script` apply owns its rollback internally, so this is a
    no-op (the script has already restored + exited non-zero -> "rolled-back")."""
    rb = (comp.apply or {}).get("rollback_cmd")
    if not rb:
        return lambda: None
    cmd = rb.replace("{prev}", prev)

    def _run() -> None:
        try:
            res = subprocess.run(["bash", "-c", cmd], check=False, timeout=600,
                                 capture_output=True, text=True)
        except (subprocess.SubprocessError, OSError) as exc:
            raise RollbackFailed(f"{comp.name}: rollback could not run: {exc}") from exc
        if res.returncode != 0:
            tail = (res.stderr or res.stdout or "").strip().splitlines()[-1:] or [""]
            raise RollbackFailed(
                f"{comp.name}: rollback exited {res.returncode}; the prior "
                f"version was NOT restored: {tail[0][:200]}")

    return _run


MAX_AUTO_RETRIES = 3  # circuit breaker: stop re-applying a persistently-broken auto release


def _auto_due(entry: dict) -> bool:
    """Should the timer apply this auto component now? First attempt
    (pending-auto) always; a prior failure retries until MAX_AUTO_RETRIES, then
    stops (a new upstream version resets the counter in build_state)."""
    status = entry.get("status")
    if status == "pending-auto":
        return True
    if status == "failed":
        return entry.get("fail_count", 0) < MAX_AUTO_RETRIES
    return False


def cmd_apply(args, components: list[Component], state_path: Path) -> int:
    by_name = {c.name: c for c in components}
    if args.auto:
        # Delta-gate against the last check: apply only auto components that lag,
        # and stop retrying a release that keeps failing (circuit breaker).
        st: dict = {}
        if state_path.exists():
            try:
                st = json.loads(state_path.read_text(encoding="utf-8"))
            # UnicodeDecodeError is a sibling of JSONDecodeError, not a
            # subclass; one corrupt byte used to raise out of `--auto`. An
            # unreadable state file means the delta-gate has nothing to gate on.
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                print(f"state unreadable ({exc}); no prior state", file=sys.stderr)
                st = {}
        comps_state = st.get("components", {})
        targets = [c for c in components if c.tier == "auto"
                   and _auto_due(comps_state.get(c.name, {}))]
    elif args.name:
        if args.name not in by_name:
            print(f"unknown component: {args.name}")
            return 1
        targets = [by_name[args.name]]
    else:
        print("apply needs a component name or --auto")
        return 1

    results: dict[str, str] = {}
    after: dict[str, tuple[Component, str]] = {}
    for comp in targets:
        # Isolate each component: one unexpected error must not abort the batch or
        # skip _mark_state (which would hide the failure from the fail_count breaker).
        try:
            prev = resolve_current(comp)            # captured before the swap
            rollback = _build_rollback(comp, prev)
            # apply_one owns both failure paths: an apply-command failure/timeout
            # and a health-gate failure both invoke `rollback` -> "rolled-back",
            # or "rollback-failed" when the restore command itself did not work.
            result = apply_one(comp, applier=_default_applier(comp), rollback=rollback)
            if result == "skipped" and comp.apply is None:
                # Say why. A `notify` or `observed` component with no `apply`
                # is watched only: its update is a deliberate act elsewhere (a
                # yard for the engine venv, sudo for apt, HELM for a binary a
                # service runs), and a bare "skipped" read as a broken command.
                print(f"{comp.name}: skipped, the registry gives it no apply; "
                      f"it is watched here and updated by hand")
            else:
                print(f"{comp.name}: {result}")
            if result != "skipped":
                # Whatever the outcome, ask the component's own probe what is
                # installed NOW, so the state says what is there rather than
                # what was there before the swap. `resolve_current` answers ""
                # on any failure and never raises.
                after[comp.name] = (comp, resolve_current(comp))
        except Exception as exc:  # noqa: BLE001 - boundary; one component's failure is contained
            # "error", never "rolled-back": nothing here calls `rollback()`, so
            # the component is in an UNKNOWN state (measured 2026-08-30).
            result = "error"
            print(f"{comp.name}: error ({type(exc).__name__}: {exc})")
        results[comp.name] = result
    _mark_state(state_path, results, after)
    return 1 if any(r in FAILED_RESULTS for r in results.values()) else 0


# Every outcome that must set status=failed and raise the exit code. Named once
# so a new outcome cannot be added to `apply_one` and then be invisible to BOTH
# the state file and the exit code, which is what a bare
# `result == "rolled-back"` in two places invites.
FAILED_RESULTS = frozenset({"rolled-back", "rollback-failed", "error"})


def _mark_state(state_path: Path, results: dict[str, str],
                after: dict[str, tuple[Component, str]] | None = None) -> None:
    """Record each attempted component's outcome in the state file.

    `after` holds each component and the version its own `current` probe read once
    the attempt was over, and it replaces the pre-apply reading whatever the
    outcome. A failure is still `failed` with its breaker count raised, so
    /prime surfaces it; anything else is classified afresh against the
    recorded `latest`. Until 2026-10-01 only failures were written and an
    applied component kept the version it had BEFORE the swap until the next
    day's `check`: MEASURED that day, the 07:00 timer applied cliproxyapi
    8.0.7 and `status` went on printing `8.0.4  8.0.7  pending-auto`.
    """
    after = after or {}
    if not state_path.exists():
        return
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    # Same handler as the `--auto` read; here it runs after the appliers, so an
    # escape would lose the exit code.
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"state unreadable ({exc}); outcomes not recorded", file=sys.stderr)
        return
    changed = False
    for name, result in results.items():
        entry = state.get("components", {}).get(name)
        if entry is None:
            continue
        comp, now = after.get(name, (None, ""))
        if comp is not None:
            entry["current"] = now
            entry["delta"] = versions_differ(now, entry.get("latest", ""))
            changed = True
        if result in FAILED_RESULTS:
            entry["status"] = "failed"
            entry["fail_count"] = entry.get("fail_count", 0) + 1
            changed = True
        elif comp is not None:
            entry["status"], entry["fail_count"] = classify(
                comp, now, entry.get("latest", ""), entry.get("fail_count", 0))
    if changed:
        write_state(state, state_path)   # shared atomic writer from update_common


# ============================================================
# Self-test: invented components in a temporary tree, no network
# ============================================================

# Bare command names a registry command may start with: tools a systemd user
# unit finds on its default PATH (/usr/bin, /snap/bin). Anything else has to be
# spelled out under $HOME or as an absolute path, because the 07:00 timer does
# not see the login shell's PATH; npm, nvm and node are reached only after the
# command itself sources nvm.sh.
TIMER_PATH_TOOLS = frozenset({"apt-cache", "cat", "dpkg-query", "head", "snap"})
_NVM_TOOLS = frozenset({"nvm", "npm", "node"})


def bare_commands(components: list[Component]) -> list[str]:
    """`name.field: word` for every command word the timer's PATH cannot find."""
    import re  # noqa: PLC0415
    found: list[str] = []
    for comp in components:
        blocks = {"current": comp.current, "latest": comp.latest,
                  "health": comp.health or {}, "apply": comp.apply or {}}
        for field, blk in blocks.items():
            for key in ("cmd", "rollback_cmd"):
                cmd = blk.get(key)
                if not isinstance(cmd, str):
                    continue
                sources_nvm = cmd.lstrip().startswith(". ") and "nvm.sh" in cmd
                for part in re.split(r"&&|\|\||\||;", cmd):
                    words = part.split()
                    if not words:
                        continue
                    w = words[0]
                    if (w.startswith(("$HOME/", "/")) or w == "."
                            or w in TIMER_PATH_TOOLS or (sources_nvm and w in _NVM_TOOLS)):
                        continue
                    found.append(f"{comp.name}.{field}.{key}: {w}")
    return found


def self_test() -> int:
    """The apply path's record of what is installed, driven through `cmd_apply`."""
    import argparse  # noqa: PLC0415
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    import shlex  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    from scripts.utils.data_path import data_write_dir  # noqa: PLC0415
    from scripts.utils.update_registry import load_registry  # noqa: PLC0415

    cases: list[tuple[str, bool]] = []
    with tempfile.TemporaryDirectory(dir=data_write_dir("runtime:scratch/tmp")) as tmp:
        root = Path(tmp)

        def comp(name: str, apply_cmd: str, health: str = "true",
                 probe: str | None = None) -> Component:
            ver = shlex.quote(str(root / f"{name}.ver"))
            (root / f"{name}.ver").write_text("1.0.0\n", encoding="utf-8")
            return Component(
                name=name, tier="auto", display=name,
                current={"via": "shell", "cmd": probe or f"cat {ver}"},
                latest={"via": "pypi", "package": "example-invented"},
                apply={"cmd": apply_cmd.replace("{f}", ver),
                       "rollback_cmd": f"echo {{prev}} > {ver}"},
                health={"cmd": health})

        comps = [
            comp("ok-tool", "echo 1.1.0 > {f}"),
            comp("bad-tool", "echo 1.1.0 > {f}", health="false"),
            comp("noop-tool", "true"),
            comp("mute-tool", "echo 1.1.0 > {f}", probe="true"),
        ]
        state = {"generated": "2026-01-01T00:00:00+00:00", "components": {
            c.name: {"display": c.name, "tier": "auto", "current": "1.0.0",
                     "latest": "1.1.0", "delta": True, "status": "pending-auto",
                     "fail_count": 0} for c in comps}}
        sp = root / "state.json"
        write_state(state, sp)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = cmd_apply(argparse.Namespace(auto=True, name=None), comps, sp)
        got = json.loads(sp.read_text(encoding="utf-8"))["components"]
        ok, bad, noop, mute = (got[c.name] for c in comps)
        cases.append(("an applied update records the version read after it, as current",
                      ok["current"] == "1.1.0" and ok["status"] == "current"
                      and ok["delta"] is False and ok["fail_count"] == 0))
        cases.append(("a rolled-back update records the restored version and stays failed",
                      bad["current"] == "1.0.0" and bad["status"] == "failed"
                      and bad["fail_count"] == 1 and rc == 1))
        cases.append(("an apply that changed nothing still reads as lagging, never current",
                      noop["current"] == "1.0.0" and noop["status"] == "pending-auto"))
        cases.append(("a probe that answers nothing after apply records unknown, not the old version",
                      mute["current"] == "" and mute["status"] == "unknown"))

    shipped = load_registry(Path(__file__).resolve().parent.parent.parent
                            / "config" / "update-registry.yaml")
    invented = [Component(name="example", tier="auto", display="example",
                          current={"via": "shell", "cmd": "example-tool --version"},
                          latest={"via": "pypi", "package": "example"},
                          apply={"cmd": "$HOME/.local/bin/uv tool upgrade example",
                                 "rollback_cmd": "uv tool install example"})]
    cases.append(("a bare command name the timer's PATH cannot find is named",
                  bare_commands(invented) == ["example.current.cmd: example-tool",
                                              "example.apply.rollback_cmd: uv"]))
    stray = bare_commands(shipped)
    cases.append((f"the shipped registry names no command the timer cannot find"
                  f"{': ' + ', '.join(stray) if stray else ''}",
                  bool(shipped) and not stray))

    failures = 0
    for label, passed in cases:
        failures += not passed
        print(f"{'ok  ' if passed else 'FAIL'} {label}")
    print(f"{len(cases) - failures}/{len(cases)} passed")
    return 1 if failures else 0
