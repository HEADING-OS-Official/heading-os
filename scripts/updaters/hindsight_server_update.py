#!/usr/bin/env python3
"""Apply script for the `hindsight-server` update-manager component (HOS-359).

Automates the manual upgrade that ran clean on 2026-10-23 (1.0.0 -> 1.1.0) and
2026-11-13 (1.1.0 -> 1.2.1), recorded in the data repository. Over the ssh
alias in `.env` `HINDSIGHT_ADMIN_SSH_HOST` (root on the memory server):

  a. versions and the shared Hindsight lock (`hindsight_common`);
  b. resolve tag v<latest> to its commit through the GitHub API, clone it beside
     the running tree as /opt/hindsight/v<latest> (or reuse a tree already
     there only when its HEAD is that commit), build it with uv, chown it like
     the running tree; the services keep running;
  c. both unit files must name /opt/hindsight/v<current>/;
  d. no stop unless the laptop's `hindsight-sync.service` positively reads as
     inactive or failed, none once more than 600 s have passed since start,
     and none unless every per-call limit after the stop still fits the
     registry's `timeout_s` (the build is kept; each is exit 0 "not now");
  e. stop worker and API, pgBackRest diff backup with nothing writing; a failed
     backup starts the old services again;
  f. migrate with the NEW tree's `hindsight-admin`; a failed migration leaves
     both services STOPPED and notifies (old code on a migrated schema is not
     proven safe, per the 1.1.0 plan);
  g. back up and repoint both unit files, start, wait up to 120 s for
     `/version` to report <latest> and `/health` healthy;
  h. a health failure after the migration stops both services and leaves them
     stopped, units on the new version, backups beside them, and notifies.

The database is never restored automatically, and no old tree, backup file or
pgBackRest backup is ever deleted.

Exit codes: 0 applied, already current, or not now (lock busy, sync running,
time cutoff passed); 1 failed or refused, the printed line names the state left.

Usage:
  .venv/bin/python scripts/updaters/hindsight_server_update.py
  .venv/bin/python scripts/updaters/hindsight_server_update.py --dry-run --current 1.2.1 --latest 1.2.2
"""
from __future__ import annotations

import re
import shlex
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from scripts.updaters import hindsight_common as hc  # noqa: E402

NAME = "hindsight-server"
TREE_ROOT = "/opt/hindsight"
UNIT_DIR = "/etc/systemd/system"
UNITS = ("hindsight-api", "hindsight-worker")
SYNC_UNIT = "hindsight-sync.service"
SWAP_CUTOFF_S = 600
HEALTH_WAIT_S = 120
HEALTH_POLL_S = 5
# The only answers of `systemctl --user is-active` that let the stop go ahead.
# Anything else (a busy state, empty output, no user bus, a missing systemctl,
# a timeout) is "not now": a healthy inactive unit also exits non-zero (3), so
# the return code alone cannot decide it.
SYNC_IDLE = {"inactive", "failed"}
LABEL_RE = re.compile(r"\b(\d{8}-\d{6}F(?:_\d{8}-\d{6}[DI])?)\b")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Per-call timeouts.
T_SHORT = 60
T_CLONE = 300
T_BUILD = 1500
T_STOP = 120
T_BACKUP = 800
T_MIGRATE = 500

# The worst case from the stop to the exit, every term at its limit. The
# longest path is a health failure: it runs every step below. The other
# failure paths are shorter (backup failure: 120 + 60 + 800 + 120 = 1100;
# migration failure: 1540; unit rewrite failure: at most 1780).
POST_STOP_TERMS = (
    ("stop", T_STOP),                       # 120
    ("reset-failed", T_SHORT),              # 60
    ("pgBackRest backup", T_BACKUP),        # 800
    ("pgBackRest info", T_SHORT),           # 60
    ("migration", T_MIGRATE),               # 500
    ("two unit rewrites", 2 * T_SHORT),     # 120
    ("daemon-reload and start", T_STOP),    # 120
    ("health wait", HEALTH_WAIT_S + 1),     # 121, probes capped by the window
    ("stop after failed health", T_STOP),   # 120
)                                           # = 2021, plus the notification
POST_STOP_S = sum(s for _, s in POST_STOP_TERMS)
# The registry gives this script `timeout_s: 3000`. The stop starts by
# SWAP_CUTOFF_S = 600 at the latest, so the worst end is 600 + 2021 + the
# notification (60 s per own sink, 120 for two) = 2741, plus the 120 s margin
# = 2861 < 3000. `Budget.require` checks the same sum again with the real
# elapsed time and sink count right before the stop, and answers "not now"
# when it does not fit, so the bound holds even if a constant here drifts.
# Before the stop nothing is live: a kill there leaves the services running.


def _commit_for_tag(run: hc.Runner, repo: str, tag: str) -> str:
    """The commit `tag` points at, dereferencing an annotated tag."""
    if run.dry:
        run.note(f"GET https://api.github.com/repos/{repo}/git/ref/tags/{tag} "
                 "(dereference an annotated tag through /git/tags/<sha>)")
        return "<commit from GitHub API>"
    ref = hc.github_json(f"https://api.github.com/repos/{repo}/git/ref/tags/{tag}")
    obj = ref.get("object") if isinstance(ref.get("object"), dict) else {}
    for _ in range(3):
        kind, sha = obj.get("type"), obj.get("sha")
        if not isinstance(sha, str) or not SHA_RE.match(sha):
            raise hc.Outcome(1, f"GitHub API gave no commit for {tag}; nothing changed")
        if kind == "commit":
            return sha
        if kind != "tag":
            raise hc.Outcome(1, f"tag {tag} points at a {kind!r}; nothing changed")
        tag_obj = hc.github_json(f"https://api.github.com/repos/{repo}/git/tags/{sha}")
        obj = tag_obj.get("object") if isinstance(tag_obj.get("object"), dict) else {}
    raise hc.Outcome(1, f"tag {tag} does not resolve to a commit; nothing changed")


def _head(run: hc.Runner, tree: str, commit: str) -> str:
    q = shlex.quote(tree)
    res = run.remote(f"git -c safe.directory={q} -C {q} rev-parse HEAD", T_SHORT,
                     dry_out=commit)
    return res.out.strip() if res.ok else ""


def _units_on(run: hc.Runner, current: str, ts: str) -> None:
    """Both unit files name the current tree, and no backup name is taken."""
    want = f"{TREE_ROOT}/v{current}/"
    for unit in UNITS:
        path = f"{UNIT_DIR}/{unit}.service"
        res = run.remote(f"cat {shlex.quote(path)}", T_SHORT, dry_out=want)
        if not res.ok:
            raise hc.Outcome(1, f"cannot read {path} ({res.why()}); nothing stopped")
        if want not in res.out:
            raise hc.Outcome(1, f"{path} does not run {want}; nothing stopped")
        backup = hc.backup_name(path, ts, current)
        if hc.remote_exists(run, backup):
            raise hc.Outcome(1, f"{backup} already exists; nothing stopped")


def _sync_idle(run: hc.Runner) -> str:
    """"" when the laptop sync positively reads as not running, else what was
    read. Fails closed: only `inactive` or `failed` lets the stop go ahead."""
    res = run.local(["systemctl", "--user", "is-active", SYNC_UNIT], T_SHORT,
                    dry_out="inactive")
    word = res.out.strip()
    if word in SYNC_IDLE:
        return ""
    return f"{SYNC_UNIT} is-active read {word or 'nothing'!r} ({res.why()})"


def _start_old(run: hc.Runner) -> hc.Result:
    return run.remote("systemctl start " + " ".join(UNITS), T_STOP)


def _stop(run: hc.Runner) -> hc.Result:
    return run.remote("systemctl stop " + " ".join(reversed(UNITS)), T_STOP)


def _health(run: hc.Runner, latest: str) -> bool:
    probe = ("curl -s -m 5 127.0.0.1:8888/version; echo; "
             "curl -s -m 5 127.0.0.1:8888/health")
    fake = f'{{"api_version": "{latest}"}}\n{{"status":"healthy"}}'

    def check(timeout: int) -> bool:
        res = run.remote(probe, timeout, dry_out=fake)
        m = re.search(r'"api_version"\s*:\s*"([^"]+)"', res.out)
        healthy = re.search(r'"status"\s*:\s*"healthy"', res.out)
        return bool(m and hc._normalise_version(m.group(1)) == latest and healthy)

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
    repo = hc.safe_repo(comp.latest.get("repo", ""))
    ts = hc.stamp()
    old_tree = f"{TREE_ROOT}/v{current}"
    new_tree = f"{TREE_ROOT}/v{latest}"
    q_new, q_old = shlex.quote(new_tree), shlex.quote(old_tree)
    vers = f"{current} -> {latest}"

    def down(line: str) -> hc.Outcome:
        return hc.Outcome(1, line, f"Hindsight server update {vers}: {line}")

    # Read-only checks first, so a refusal here has changed nothing at all.
    _units_on(run, current, ts)
    if not hc.remote_exists(run, old_tree, dry_present=True):
        raise hc.Outcome(1, f"{old_tree} is not on the server; nothing changed")

    # b. Fetch and build beside the running tree.
    commit = _commit_for_tag(run, repo, f"v{latest}")
    if hc.remote_exists(run, new_tree):
        head = _head(run, new_tree, commit)
        if head != commit:
            raise hc.Outcome(1, f"{new_tree} exists at {head or 'an unreadable HEAD'}, "
                                f"not {commit}; nothing changed")
    else:
        res = run.remote(f"git clone --branch {shlex.quote('v' + latest)} --depth 1 "
                         f"{shlex.quote(f'https://github.com/{repo}')} {q_new}", T_CLONE)
        if not res.ok:
            raise down(f"clone of v{latest} failed ({res.why()}); services untouched")
        head = _head(run, new_tree, commit)
        if head != commit:
            raise down(f"{new_tree} HEAD is {head or 'unreadable'}, not {commit}; "
                       "tree kept for a human, services untouched")
    res = run.remote(f"cd {q_new} && UV_PYTHON_INSTALL_DIR=/opt/python /opt/uv/uv sync "
                     "--locked --package hindsight-api-slim --extra local-ml --no-dev", T_BUILD)
    if not res.ok:
        raise down(f"build of {new_tree} failed ({res.why()}); services untouched")
    res = run.remote(f"chown -R --reference={q_old} {q_new}", T_SHORT)
    if not res.ok:
        raise down(f"chown of {new_tree} failed ({res.why()}); services untouched")

    # c. Re-read the units right before the stop.
    _units_on(run, current, ts)

    # d. Not now: the laptop sync is not positively idle, the cutoff has
    # passed, or the rest does not fit the manager's timeout. Checked last,
    # right before the stop, so nothing slow runs between them and the stop.
    busy = _sync_idle(run)
    if busy:
        raise hc.Outcome(0, f"{new_tree} built; sync not known to be idle, not now: {busy}")
    if not run.dry and time.monotonic() - hc.STARTED > SWAP_CUTOFF_S:
        raise hc.Outcome(0, f"{new_tree} built; swap not now, past the {SWAP_CUTOFF_S} s cutoff")
    budget.require(POST_STOP_S + hc.notify_worst_s(), "the stop",
                   " + ".join(f"{n} {v}" for n, v in POST_STOP_TERMS)
                   + f" + notification {hc.notify_worst_s()}")

    # e. Stop, then back up with nothing writing.
    res = _stop(run)
    if not res.ok:
        started_again = _start_old(run)
        raise down(f"stop failed ({res.why()}); old services start "
                   f"{'answered' if started_again.ok else 'FAILED'}; schema untouched")
    run.remote("systemctl reset-failed " + " ".join(UNITS) + " 2>/dev/null || true", T_SHORT)
    res = run.remote("sudo -u postgres pgbackrest --stanza=hindsight --type=diff backup", T_BACKUP)
    if not res.ok:
        started_again = _start_old(run)
        raise down(f"pgBackRest backup failed ({res.why()}); old services start "
                   f"{'answered' if started_again.ok else 'FAILED'}; schema untouched")
    info = run.remote("sudo -u postgres pgbackrest --stanza=hindsight info", T_SHORT,
                      dry_out="diff backup: <label from pgbackrest info>")
    labels = LABEL_RE.findall(info.out) if info.ok else []
    label = labels[-1] if labels else ("<label from pgbackrest info>" if run.dry else "unread")
    print(f"pgBackRest backup before migration: {label}")

    # f. Migrate with the new tree.
    res = run.remote(
        "systemd-run --wait --pipe --collect --uid=hindsight "
        "-p WorkingDirectory=/var/empty/hindsight "
        "-p EnvironmentFile=/etc/hindsight/runtime.env "
        "-p EnvironmentFile=/etc/hindsight/secrets.env "
        f"{shlex.quote(new_tree + '/.venv/bin/hindsight-admin')} run-db-migration "
        "--embedding-dimension 1024", T_MIGRATE)
    if not res.ok:
        raise down(f"the memory server is DOWN: migration failed ({res.why()}); both services "
                   f"left stopped; pgBackRest backup {label}; a human decides between "
                   "restore and repair")

    # g. Back up and repoint the units, start, wait for health.
    for unit in UNITS:
        path = f"{UNIT_DIR}/{unit}.service"
        backup = hc.backup_name(path, ts, current)
        res = run.remote(hc.keep_copy_cmd(path, backup) + " && "
                         + hc.sed_path_swap(old_tree + "/", new_tree + "/", path), T_SHORT)
        if not res.ok:
            _stop(run)
            raise down(f"the memory server is DOWN: repointing {path} failed ({res.why()}); "
                       f"schema migrated, database NOT restored; pgBackRest backup {label}")
    res = run.remote("systemctl daemon-reload && systemctl start " + " ".join(UNITS), T_STOP)
    if res.ok and _health(run, latest):
        raise hc.Outcome(0, f"{NAME}: applied {vers}; /version {latest}, /health healthy; "
                            f"pgBackRest backup {label}")

    # h. Health failed after the migration: stop and leave stopped.
    _stop(run)
    raise down(f"the memory server is DOWN: {latest} did not become healthy within "
               f"{HEALTH_WAIT_S} s; both services stopped, units on {latest} with backups "
               f"beside them, schema migrated, database NOT restored; pgBackRest backup {label}")


def main() -> int:
    args = hc.base_parser(__doc__.splitlines()[0]).parse_args()
    return hc.finish(lambda: run_update(args), NAME, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
