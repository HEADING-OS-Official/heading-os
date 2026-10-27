#!/usr/bin/env python3
"""Workspace update manager: inventory external components and apply updates
under a tiered autonomy model (auto / notify / observed).

Usage:
    python scripts/update-manager.py check          # poll latest, write state
    python scripts/update-manager.py status         # print the table, dated
    python scripts/update-manager.py apply <name>   # apply one notify/auto
    python scripts/update-manager.py apply --auto    # apply all auto-tier
    python scripts/update-manager.py docs [--force]  # documentation watch, every 14 days
    python scripts/update-manager.py self-test       # docs watch + apply scenarios, no network

Registry: config/update-registry.yaml. State: the `update-state` registry name.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, timezone  # noqa: E402
from scripts.utils.update_registry import Component, RegistryError, load_registry  # noqa: E402
from scripts.utils.update_common import (  # noqa: E402
    CELL_NEVER_CHECKED, CELL_NOT_ANSWERED, UNDATED,
    classify, reading_freshness, resolve_current, versions_differ, write_state,
)
from scripts.utils import update_sources  # noqa: E402
from scripts.utils import update_docwatch  # noqa: E402
from scripts.utils.workspace import get_workspace_root  # noqa: E402
from scripts.utils.data_path import UnknownDataPath, data_path, data_write_dir  # noqa: E402
from scripts.utils.clone_guard import require_main_clone


def registry_path() -> Path:
    return get_workspace_root() / "config" / "update-registry.yaml"


def state_path() -> Path:
    """The `update-state` record, `state.json`, through the front door.

    The front door does not fall back: on a tree whose registry lacks the name
    (a public clone with no data repository) this raises `UnknownDataPath`
    rather than answering a vacated legacy directory (a live writer still naming
    the old address refilled it, MEASURED 2026-09-22). This script refuses
    outside the main clone anyway, at `require_main_clone` in `main`.
    """
    return data_path('update-state')


def resolve_latest(comp: Component, current: str = "") -> str:
    try:
        return update_sources.latest_version(comp.latest, current)
    except update_sources.SourceError as exc:
        # Say it. Swallowed with no message, a total upstream outage was
        # indistinguishable from "everything is up to date": `build_state`
        # recorded status "unknown" per component and `check` printed
        # "checked N components; 0 waiting" either way.
        print(f"[update-manager] {comp.name}: could not resolve latest version "
              f"({exc})", file=sys.stderr)
        return ""


def build_state(components: list[Component], prior: dict | None = None) -> dict:
    prior_comp = (prior or {}).get("components", {})
    entries: dict[str, dict] = {}
    for comp in components:
        current = resolve_current(comp)
        latest = resolve_latest(comp, current)
        delta = versions_differ(current, latest)
        prior_e = prior_comp.get(comp.name, {})
        fail_count = prior_e.get("fail_count", 0)
        # A NEW upstream version resets the circuit breaker. Guard on `latest`
        # being truthy so a transient resolve failure (empty latest) does NOT
        # reset it. Breaker memory lives in fail_count, so it survives a cycle
        # where this component transiently resolves to `unknown`.
        if latest and prior_e.get("latest") and prior_e.get("latest") != latest:
            fail_count = 0
        status, fail_count = classify(comp, current, latest, fail_count)
        entries[comp.name] = {
            "display": comp.display, "tier": comp.tier,
            "current": current, "latest": latest,
            "delta": delta, "status": status, "fail_count": fail_count,
        }
    return {"generated": None, "components": entries}


def _stamp_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_state() -> dict:
    p = state_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    # UnicodeDecodeError sits between the two named here and is caught by
    # neither: it is a ValueError raised INSIDE `read_text`, a sibling of
    # `json.JSONDecodeError` and not a subclass of OSError, so `json.loads` is
    # never reached. A state file with one torn byte therefore crashed every
    # update-manager subcommand out of a function whose whole contract is to
    # answer `{}` when the state cannot be read.
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def cmd_check(_args) -> int:
    comps = load_registry(registry_path())
    state = build_state(comps, _read_state())
    state["generated"] = _stamp_now()
    write_state(state, state_path())
    waiting = [n for n, e in state["components"].items() if e["status"] == "waiting"]
    unknown = [n for n, e in state["components"].items() if e["status"] == "unknown"]
    line = f"checked {len(comps)} components; {len(waiting)} waiting"
    # Name them. "0 waiting" over a set nothing resolved asserts a state the
    # run never established, which is what `.claude/rules/scope-claims.md`
    # forbids. And name WHICH probe gave nothing: until 2026-10-01 every
    # `unknown` was called an upstream not reached, and MEASURED that day the
    # two it named (yt-dlp, claude-cli) had answered upstream; it was their
    # LOCAL version command that found no binary on the timer's PATH.
    no_local = [n for n in unknown if not state["components"][n]["current"]]
    no_upstream = [n for n in unknown if not state["components"][n]["latest"]]
    if no_local:
        line += f"; {len(no_local)} installed version(s) NOT read: {', '.join(no_local)}"
    if no_upstream:
        line += f"; {len(no_upstream)} upstream(s) NOT reached: {', '.join(no_upstream)}"
    print(line)
    # The documentation watch rides the same daily timer and returns at once
    # when it is not due. Its outcome is reported, never folded into this
    # command's exit code: a docs page that could not be fetched is a line in
    # its report and in `/prime`, and must not stop `apply --auto`.
    _docs_run(force=False)
    # Nothing resolved means nothing was checked, whatever the first clause of
    # that sentence says. Degrade non-zero, per the console-first rule, so the
    # timer's `check && apply --auto` stops and the unit shows the failure.
    if comps and len(unknown) == len(comps):
        print("no upstream resolved; the version data is unchanged from the "
              "previous run", file=sys.stderr)
        return 1
    return 0


def _docs_paths() -> update_docwatch.Paths:
    """The watch's three addresses in the data repository, by registry name."""
    return update_docwatch.Paths(
        state=data_path("doc-watch-state"),
        snapshots=data_path("doc-watch-snapshots"),
        reports=data_path("doc-watch-reports"),
    )


def _docs_run(force: bool) -> int:
    """Run the documentation watch if due (or forced); say what happened."""
    try:
        sources = update_docwatch.load_doc_sources(registry_path())
        paths = _docs_paths()
    except (update_docwatch.DocSourceError, UnknownDataPath) as exc:
        print(f"[doc-watch] not run: {exc}", file=sys.stderr)
        return 1
    state = update_docwatch.run(sources, paths, force=force)
    if state is None:
        print("[doc-watch] not due (runs every 14 days; --force to run now)")
        return 0
    line = update_docwatch.prime_line(state)
    print(f"[doc-watch] {line or 'no changes, every source checked'}; report: {state['report']}")
    return 0


def cmd_docs(args) -> int:
    return _docs_run(force=args.force)


def _registry_by_name() -> tuple[dict[str, Component] | None, str]:
    """The registry keyed by component name, or `(None, reason)`.

    `status` needs it for the one question the state file cannot answer: a
    component listed in the registry and absent from the state has never been
    probed at all, which calls for a `check`, while a component whose entry
    holds an empty version was probed and gave nothing, which calls for
    repairing the probe. When the registry cannot be read, that question goes
    unanswered OUT LOUD rather than being guessed either way.
    """
    try:
        return {c.name: c for c in load_registry(registry_path())}, ""
    except RegistryError as exc:
        return None, str(exc)


def _cell(value: str) -> str:
    """A version cell, or the word for why it holds no version.

    An empty cell was the defect: MEASURED 2026-09-20, two blank `current`
    cells were read as two broken probes and filed as such, when the probes
    were fine and the RECORD was two days old. Blank says nothing, so it is
    read as whatever the reader already suspects.
    """
    return value if value else CELL_NOT_ANSWERED


def _not_answered_reason(comp: Component | None, field: str) -> str:
    """Why one cell holds no version, in the words the registry supports.

    `build_state` probes both fields for every registry component on every
    run, so an entry that exists with an empty field IS a probe that ran and
    returned nothing. Without the registry the source cannot be named, and the
    sentence says only what is then established.
    """
    if comp is None:
        return f"{field}: the probe returned no version (registry unreadable, "\
               f"so the source it queried cannot be named here)"
    if field == "current":
        return "current: the local version command returned no version string"
    via = (comp.latest or {}).get("via", "upstream")
    return f"latest: the {via} source did not answer"


def cmd_status(_args) -> int:
    # `_read_state()`, not a second raw `json.loads`. The copy this replaces
    # crashed `status` with a JSONDecodeError traceback on a truncated state
    # file, while `check` -- reading the same file through `_read_state` --
    # degraded politely. The operator-facing command was the fragile one.
    state = _read_state()
    if not state:
        # The reading is UNDATED and empty at once, and both halves are said.
        print("reading: AGE UNKNOWN, there is no readable state file, so no "
              "version below has ever been measured; "
              "re-measure: python scripts/update-manager.py check")
        return 1
    freshness = reading_freshness(state, get_workspace_root())
    print(freshness["line"])
    entries = state.get("components") or {}
    registry, registry_error = _registry_by_name()
    if registry is None:
        print(f"note: {registry_path().name} could not be read ({registry_error}); "
              f"a component that has never been checked cannot be told from one "
              f"that was removed, so only recorded components are listed below")
    # Registry order first, so a newly registered component is VISIBLE as never
    # checked instead of being absent from the table entirely; then anything the
    # state still carries that the registry no longer lists.
    order = list(registry or {})
    order += [n for n in entries if n not in order]
    if not order:
        if registry is None or freshness["state"] == UNDATED:
            # Nothing recorded, and no dated reading or no readable registry to
            # say whether that is correct. Not the same as an empty registry.
            print("no components recorded, and nothing here establishes whether "
                  "that is right; run: python scripts/update-manager.py check")
            return 1
        # Empty AND successful, which is a third thing again: the check ran, at
        # the time printed above, over a registry that tracks nothing.
        print("no components to track: the check above ran against an empty registry")
        return 0
    print(f"{'component':22} {'current':14} {'latest':14} {'tier':9} status")
    unanswered: list[str] = []
    never: list[str] = []
    for name in order:
        e = entries.get(name)
        comp = (registry or {}).get(name)
        if e is None:
            never.append(name)
            tier = comp.tier if comp else "?"
            print(f"{name:22} {CELL_NEVER_CHECKED:14} {CELL_NEVER_CHECKED:14} "
                  f"{tier:9} {CELL_NEVER_CHECKED}")
            continue
        current, latest = e.get("current", ""), e.get("latest", "")
        for field, value in (("current", current), ("latest", latest)):
            if not value:
                unanswered.append(f"{name:22} {_not_answered_reason(comp, field)}")
        print(f"{name:22} {_cell(current):14} {_cell(latest):14} "
              f"{e.get('tier', '?'):9} {e.get('status', '?')}")
    if unanswered:
        print(f"\n{CELL_NOT_ANSWERED}: the probe ran when the reading above was "
              f"taken, and returned nothing:")
        for line in unanswered:
            print(f"  {line}")
    if never:
        print(f"\n{CELL_NEVER_CHECKED}: listed in {registry_path().name} and "
              f"absent from the state file, so no probe has run for it yet:")
        for name in never:
            print(f"  {name}")
    # Non-zero when the whole table is un-measured, because the command then
    # reports nothing about any version. A reading that is merely STALE exits 0:
    # it is a successful report, and the line above carries the age.
    return 1 if len(never) == len(order) else 0


def _import_fcntl():
    """The advisory-lock module, or None on a platform without it.

    A named seam rather than an inline `if` so the no-lock path can be
    exercised. Patching `os.name` in a test is not an option: `pathlib` picks
    WindowsPath from it and the interpreter then refuses to build a Path at all.
    That is why the degraded branch below went untested and shipped broken.
    """
    if os.name == "posix":
        import fcntl  # noqa: PLC0415
        return fcntl
    return None


def apply_lock_path() -> Path:
    """`.apply.lock` in the registry name `update-manager-locks`, created.

    The lock is runtime state (a fresh clone needs no memory of a past apply);
    `state.json` is a record and lives at `state_path()`. Its own seam, so a
    caller can pin it to a temporary tree instead of the operator's live store.
    """
    return data_write_dir("update-manager-locks") / ".apply.lock"


def main(argv: list[str] | None = None) -> int:
    # `self-test` runs before the clone guard on purpose: it builds its own
    # temporary tree, touches no network and nothing of the operator's, so a
    # yard can prove the watch before its branch is merged.
    if (sys.argv[1:] if argv is None else argv) == ["self-test"]:
        from scripts.utils import update_apply  # noqa: PLC0415
        print("doc watch:")
        docs = update_docwatch.self_test()
        print("apply and registry:")
        return docs | update_apply.self_test()
    require_main_clone(__file__)
    parser = argparse.ArgumentParser(description="Workspace update manager")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    sub.add_parser("status")
    ap = sub.add_parser("apply")
    ap.add_argument("name", nargs="?")
    ap.add_argument("--auto", action="store_true")
    dp = sub.add_parser("docs")
    dp.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if args.cmd == "docs":
        return cmd_docs(args)
    if args.cmd == "check":
        return cmd_check(args)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "apply":
        # `fcntl` is UNIX-ONLY, and this repo ships ps1 installers and a
        # cross-platform schedule helper -- a hard import made the whole apply
        # tier a ModuleNotFoundError traceback on the Windows host. The lock is
        # advisory anyway: without it two concurrent applies can interleave, so
        # Windows degrades with a named warning rather than losing the command.
        fcntl = _import_fcntl()
        from scripts.utils.update_apply import cmd_apply  # noqa: PLC0415
        lock_path = apply_lock_path()
        with open(lock_path, "w") as lock_fh:
            if fcntl is None:
                # The named degradation above: without it `fcntl.flock` raised
                # AttributeError on a non-posix host (fixed 2026-08-25).
                print("no advisory lock on this platform; "
                      "do not run two applies at once")
                return cmd_apply(args, load_registry(registry_path()), state_path())
            try:
                fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # Manual apply racing the 07:00 timer (or vice versa). Skip rather
                # than interleave snapshot/swap on the same component.
                print("another apply is in progress; skipping")
                return 0
            return cmd_apply(args, load_registry(registry_path()), state_path())
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
