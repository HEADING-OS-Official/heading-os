#!/usr/bin/env python3
"""Deterministic git push that never relies on a wall-clock guess.

The engine repo's ``pre-push`` hook runs a regression gate for minutes, so a
plain ``timeout`` on ``git push`` kills the gate and reads as a network hang.
This wrapper drives the push
through ``scripts/utils/supervise.run_supervised``: it blocks until the push
*verifiably* finishes, declaring HUNG only on inactivity (no output + no CPU
across the process tree) — never on elapsed time — and verifies the branch
actually advanced (``ahead/behind == 0 0``) before reporting success. An exit
code of 0 alone is not trusted.

Usage:
  python scripts/safe-push.py --repo engine          # push the engine repo
  python scripts/safe-push.py --repo data            # push the data overlay
  python scripts/safe-push.py --repo all             # engine, then data
  python scripts/safe-push.py --repo engine --json   # machine-readable verdict
  python scripts/safe-push.py --repo engine --stall-window 180
  python scripts/safe-push.py --repo engine \
      --force-with-lease refs/heads/main=<40-hex id origin holds now> \
      --force-with-lease refs/tags/v1.0=<40-hex id origin holds now>

``--force-with-lease`` exists for a deliberate history rewrite (HOS-59) and for
nothing else: each listed ref replaces the remote's only while the remote still
holds the id given, and is read back afterwards. Single repository only.

Auth: reads GH_TOKEN from the engine ``.env`` (same token as the ``git pushgh``
alias) and feeds it to git via a credential helper through the child *env* — the
token never appears on the command line. Verify state lives in a live JSON
status file in the data repository's runtime store (``runtime:push/state``), so
the run is fully observable when launched in the background (HOS-5 section 7);
a clone with no data repository keeps no status file and says so.

Exit codes: 0 all repos pushed (verified). 1 a test gate / push failed.
2 a run hung (killed). 3 auth/config problem. 4 push reported success but the
branch did not advance (postcondition mismatch).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.clone_guard import require_main_clone
from scripts.utils.colors import BOLD, CYAN, GRAY, GREEN, RED, RESET, YELLOW
from scripts.utils.git_push import (
    enclosing_repo_root,
    load_gh_token,
    supervised_push,
)
from scripts.utils.data_path import DataPathError, data_write_dir
from scripts.utils.workspace import get_data_root, get_workspace_root

# state -> exit code
_EXIT = {"ok": 0, "failed": 1, "hung": 2, "postcondition_failed": 4}


def _repo_path(name: str) -> Path:
    """Resolve ONE repo root, on demand.

    The dict form resolved both eagerly, so `--repo engine` -- the advertised
    standalone usage -- called `get_data_root()` and failed on a fresh clone
    with no private overlay configured, before any push was attempted.
    """
    return get_workspace_root() if name == "engine" else get_data_root()


def _no_data_overlay() -> str | None:
    """Why 'data' is not a repository on this machine, or None if it is.

    On a clone with no data repository `get_data_root()` resolves to the
    sentinel `<engine>/examples`, inside the engine clone, which `git -C` would
    resolve to the ENGINE's remote. Named here as "no data repository" rather
    than left to the remote wall, whose refusal reads as a leak.
    """
    engine = get_workspace_root().resolve()
    try:
        data = get_data_root().resolve()
    except Exception as exc:            # noqa: BLE001 - reported, never swallowed
        return f"the data root could not be resolved: {exc}"
    if data == engine:
        return (f"the data root resolves to the engine clone itself ({engine}), "
                f"so there is no separate overlay to push")
    root = enclosing_repo_root(data)
    if root is not None and root != data:
        return (f"the data root ({data}) is not a repository of its own: it sits "
                f"inside {root}. This clone has no private data overlay")
    return None


REPO_NAMES = ("engine", "data")


def _push_one(name: str, repo: Path, token: str, *, branch: str,
              remote: str, stall_window: float, status_dir: Path | None,
              force_with_lease: dict | None = None) -> dict:
    status_path = (str(status_dir / f"{name}.status.json")
                   if status_dir is not None else None)
    verdict = supervised_push(
        repo, remote=remote, branch=branch, token=token,
        stall_window=stall_window, status_path=status_path,
        label=f"push:{name}", force_with_lease=force_with_lease,
    )
    verdict["repo"] = name
    return verdict


def _print_verdict(v: dict) -> None:
    state = v["state"]
    color = {"ok": GREEN, "failed": RED, "hung": RED,
             "postcondition_failed": YELLOW}.get(state, RED)
    icon = {"ok": "PUSHED", "failed": "FAILED", "hung": "HUNG",
            "postcondition_failed": "NOT-ADVANCED"}.get(state, state.upper())
    print(f"\n{BOLD}{color}[{icon}]{RESET} {v['repo']} "
          f"{GRAY}({v['elapsed_s']}s, exit {v['exit_code']}){RESET}")
    print(f"  {v['reason']}")
    if state != "ok" and v.get("tail"):
        print(f"{GRAY}  --- last output ---{RESET}")
        for line in v["tail"].splitlines()[-12:]:
            print(f"{GRAY}  | {line}{RESET}")


def main() -> int:
    require_main_clone(__file__)
    ap = argparse.ArgumentParser(description="Deterministic supervised git push.")
    ap.add_argument("--repo", choices=["engine", "data", "all"], required=True)
    ap.add_argument("--branch", default="main")
    ap.add_argument("--remote", default="origin")
    ap.add_argument("--stall-window", type=float, default=120.0,
                    help="seconds of zero progress before declaring HUNG")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--force-with-lease", action="append", default=[],
                    metavar="REF=ID",
                    help="replace REF on the remote only if it still holds ID "
                         "(full ref name, 40-hex id); repeatable; single repo only")
    args = ap.parse_args()
    leases = {}
    for item in args.force_with_lease:
        ref, sep, oid = item.partition("=")
        if not sep:
            ap.error(f"--force-with-lease takes REF=ID, got {item!r}")
        leases[ref] = oid
    if leases and args.repo == "all":
        ap.error("--force-with-lease names refs of ONE repository; pass --repo engine or --repo data")

    token = load_gh_token()
    if not token:
        msg = "no GH_TOKEN in engine .env, cannot authenticate push"
        print(f"{RED}auth error:{RESET} {msg}", file=sys.stderr)
        if args.json:
            # A LIST of one, not a bare object. Every other exit from this
            # command prints a list of verdicts, so a consumer doing
            # `json.loads(out)[0]["state"]` crashed on exactly the path it most
            # needs to read. One documented shape, always.
            print(json.dumps([{"repo": None, "state": "auth_error",
                               "reason": msg}], indent=2))
        return 3

    try:
        status_dir = data_write_dir("runtime:push/state")
    except DataPathError as exc:
        # The push itself does not need the file; the background observer
        # does. Said once, then the push goes ahead unobserved.
        print(f"{YELLOW}no push status file on this tree ({exc}); the push "
              f"runs without one{RESET}", file=sys.stderr)
        status_dir = None
    targets = list(REPO_NAMES) if args.repo == "all" else [args.repo]

    if "data" in targets:
        why = _no_data_overlay()
        if why:
            msg = f"nothing to push for 'data': {why}."
            print(f"{RED}config error:{RESET} {msg}", file=sys.stderr)
            if args.json:
                print(json.dumps([{"repo": "data", "state": "no_data_repo",
                                   "reason": msg}], indent=2))
            return 3

    verdicts = []
    for name in targets:
        if not args.json:
            print(f"{CYAN}supervised push -> {name}{RESET} "
                  f"{GRAY}(stall-window {args.stall_window:.0f}s; HUNG only on "
                  f"no output + no CPU, never on elapsed time){RESET}")
        v = _push_one(name, _repo_path(name), token, branch=args.branch,
                      remote=args.remote, stall_window=args.stall_window,
                      status_dir=status_dir, force_with_lease=leases or None)
        verdicts.append(v)
        if not args.json:
            _print_verdict(v)
        # MUST-finish gate: never proceed to the next repo unless this one is
        # verifiably pushed.
        if v["state"] != "ok":
            break

    if args.json:
        print(json.dumps(verdicts, indent=2))

    worst = max((_EXIT.get(v["state"], 1) for v in verdicts), default=1)
    if not args.json:
        ok = all(v["state"] == "ok" for v in verdicts) and len(verdicts) == len(targets)
        print(f"\n{BOLD}{'ALL PUSHED & VERIFIED' if ok else 'STOPPED — not all verified'}{RESET}")
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
