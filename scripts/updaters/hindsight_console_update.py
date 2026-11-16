#!/usr/bin/env python3
"""Apply script and `current` probe for the `hindsight-control-plane` update-manager
component, the Hindsight web console (HOS-359).

The console runs on the memory server from /opt/hindsight-cp/<version>/package
under `hindsight-cp.service`, reached over the ssh alias in `.env`
`HINDSIGHT_ADMIN_SSH_HOST` (root on the server).

`--print-current` prints the version the unit runs, read from the
`/opt/hindsight-cp/<version>/` path in `hindsight-cp.service`; it prints
nothing on stdout and exits 1 when that cannot be read. It is the registry's
`current` probe for the console.

The apply follows the server: it runs only when the server (read through the
`hindsight-server` registry probe) already reports the console's target
version, otherwise it waits with exit 0. Then: `npm pack` the target into
/opt/hindsight-cp/<latest> on the server, compare the tarball's sha512 with the
npm registry's `dist.integrity` before unpacking, unpack, chown like the running
tree, back up and repoint the unit, restart, and wait up to 30 s for
127.0.0.1:9999/ to answer 200 or 3xx. A health failure restores the unit,
restarts the old console, exits 1 and notifies. No old tree is deleted.

Exit codes: 0 applied, already current, or not now; 1 failed or refused.

Usage:
  .venv/bin/python scripts/updaters/hindsight_console_update.py --print-current
  .venv/bin/python scripts/updaters/hindsight_console_update.py
  .venv/bin/python scripts/updaters/hindsight_console_update.py --dry-run --current 1.2.1 --latest 1.2.2
"""
from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from scripts.updaters import hindsight_common as hc  # noqa: E402

NAME = "hindsight-control-plane"
TREE_ROOT = "/opt/hindsight-cp"
UNIT = "/etc/systemd/system/hindsight-cp.service"
SERVICE = "hindsight-cp"
HEALTH_WAIT_S = 30
HEALTH_POLL_S = 3
T_SHORT = 60
T_PACK = 300
UNIT_VERSION_RE = re.compile(r"/opt/hindsight-cp/(\d+(?:\.\d+)*)/")

# The worst case from the unit swap to the exit, every term at its limit: a
# health failure runs all of them.
POST_SWAP_TERMS = (
    ("swap and restart", T_SHORT),            # 60
    ("health wait", HEALTH_WAIT_S + 1),       # 31, probes capped by the window
    ("restore and restart", T_SHORT),         # 60
    ("health wait of the old console", HEALTH_WAIT_S + 1),  # 31
)                                             # = 182, plus the notification
POST_SWAP_S = sum(s for _, s in POST_SWAP_TERMS)
# Before the swap, at the per-call limits: the `current` probe 120, the npm
# version 20, the server version 30, the npm integrity 20, the unit read 60,
# three existence checks 180, npm pack 300, sha512sum 60, unpack and chown 60
# = 850. The registry gives this script `timeout_s: 1500`: 850 + 182 + the
# notification (60 s per own sink, 120 for two) + the 120 s margin = 1272 <
# 1500. `Budget.require` checks the post-swap sum again with the real elapsed
# time right before the swap and answers "not now" when it does not fit.
# Before the swap the running console is untouched.


def print_current() -> int:
    """The console's `current` probe: one version line on stdout, or nothing
    and exit 1."""
    try:
        host = hc.admin_host(False)
    except hc.Outcome as out:
        print(out.line, file=sys.stderr)
        return 1
    res = hc.Runner(dry=False, host=host).remote(f"cat {shlex.quote(UNIT)}", T_SHORT)
    found = set(UNIT_VERSION_RE.findall(res.out)) if res.ok else set()
    if len(found) != 1:
        print(f"cannot read one console version from {UNIT} ({res.why()})", file=sys.stderr)
        return 1
    print(found.pop())
    return 0


def _http_ok(run: hc.Runner) -> bool:
    def check(timeout: int) -> bool:
        res = run.remote("curl -s -o /dev/null -w '%{http_code}' -m 5 127.0.0.1:9999/",
                         timeout, dry_out="307")
        code = res.out.strip()
        return code == "200" or re.fullmatch(r"3\d\d", code) is not None

    return hc.wait_until(check, HEALTH_WAIT_S, HEALTH_POLL_S, T_SHORT, run.dry)


def run_update(args) -> None:
    hc.check_dry_args(args)
    run = hc.Runner(dry=args.dry_run, host=hc.admin_host(args.dry_run))
    if not args.dry_run:
        _lock = hc.take_lock()  # noqa: F841 - held until the process exits
    comp = hc.component(NAME)
    budget = hc.Budget.of(comp, args.dry_run)
    current, latest = hc.read_versions(comp, args)
    hc.compare(NAME, current, latest)
    hc.server_on(latest, run.dry)
    package = comp.latest.get("package", "")
    integrity = hc.npm_integrity(package, latest, run.dry)
    ts = hc.stamp()
    old_tree, new_tree = f"{TREE_ROOT}/{current}", f"{TREE_ROOT}/{latest}"
    tarball = f"{new_tree}/{package.lstrip('@').replace('/', '-')}-{latest}.tgz"
    backup = hc.backup_name(UNIT, ts, current)
    q_new, q_unit, q_backup = shlex.quote(new_tree), shlex.quote(UNIT), shlex.quote(backup)
    vers = f"{current} -> {latest}"

    def failed(line: str) -> hc.Outcome:
        return hc.Outcome(1, line, f"Hindsight web console update {vers}: {line}")

    # Read-only checks: a refusal here has changed nothing.
    res = run.remote(f"cat {q_unit}", T_SHORT, dry_out=f"{old_tree}/package/bin/cli.js")
    if not res.ok:
        raise hc.Outcome(1, f"cannot read {UNIT} ({res.why()}); nothing changed")
    if f"{old_tree}/" not in res.out:
        raise hc.Outcome(1, f"{UNIT} does not run {old_tree}/; nothing changed")
    if not hc.remote_exists(run, old_tree, dry_present=True):
        raise hc.Outcome(1, f"{old_tree} is not on the server; nothing changed")
    if hc.remote_exists(run, new_tree):
        raise hc.Outcome(1, f"{new_tree} already exists on the server, left by an earlier "
                            "attempt; a human looks; nothing changed")
    if hc.remote_exists(run, backup):
        raise hc.Outcome(1, f"{backup} already exists; nothing changed")

    # Fetch, verify, unpack beside the running tree.
    res = run.remote(f"mkdir -p {q_new} && cd {q_new} && "
                     f"npm pack {shlex.quote(f'{package}@{latest}')}", T_PACK)
    if not res.ok:
        raise failed(f"npm pack failed ({res.why()}); console untouched")
    res = run.remote(f"sha512sum {shlex.quote(tarball)}", T_SHORT,
                     dry_out="<sha512 of the tarball> " + tarball)
    digest = res.out.split()[0] if res.ok and res.out.split() else ""
    run.note(f"compare the tarball's sha512 with {integrity}")
    matches = bool(re.fullmatch(r"[0-9a-f]{128}", digest)) and hc.sri_sha512(digest) == integrity
    if not run.dry and not matches:
        raise failed(f"tarball integrity does not match the npm registry; not unpacked; "
                     f"{tarball} kept for a human; console untouched")
    res = run.remote(f"tar --no-same-owner -xzf {shlex.quote(tarball)} -C {q_new} && "
                     f"chown -R --reference={shlex.quote(old_tree)} {q_new}", T_SHORT)
    if not res.ok:
        raise failed(f"unpack or chown of {new_tree} failed ({res.why()}); console untouched")

    # Swap the unit, restart, health; only when the rest fits the timeout.
    budget.require(POST_SWAP_S + hc.notify_worst_s(), "the unit swap",
                   " + ".join(f"{n} {v}" for n, v in POST_SWAP_TERMS)
                   + f" + notification {hc.notify_worst_s()}")
    res = run.remote(hc.keep_copy_cmd(UNIT, backup), T_SHORT)
    if not res.ok:
        raise failed(f"could not keep {UNIT} as {backup} ({res.why()}); unit unchanged, "
                     "console untouched")
    res = run.remote(hc.sed_path_swap(old_tree + "/", new_tree + "/", UNIT)
                     + f" && systemctl daemon-reload && systemctl restart {SERVICE}", T_SHORT)
    if res.ok and _http_ok(run):
        raise hc.Outcome(0, f"{NAME}: applied {vers}; 127.0.0.1:9999/ answers")

    # Restore the unit and the old console.
    back = run.remote(f"cp -p {q_backup} {q_unit} && systemctl daemon-reload && "
                      f"systemctl restart {SERVICE}", T_SHORT)
    old_up = back.ok and _http_ok(run)
    raise failed(f"{latest} did not answer on 127.0.0.1:9999/ within {HEALTH_WAIT_S} s; unit "
                 f"restored from {backup}, old console "
                 f"{'answers again' if old_up else 'DOES NOT ANSWER, the console is down'}")


def main() -> int:
    parser = hc.base_parser(__doc__.splitlines()[0])
    parser.add_argument("--print-current", action="store_true",
                        help="print the console version the unit runs (the registry probe)")
    args = parser.parse_args()
    if args.print_current:
        return print_current()
    return hc.finish(lambda: run_update(args), NAME, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
