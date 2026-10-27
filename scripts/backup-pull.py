#!/usr/bin/env python3
"""Pull a server's pgBackRest repository to this machine and keep only verified copies.

The server keeps its own encrypted pgBackRest repository and exposes it, read-only,
to a dedicated ssh user whose `authorized_keys` line forces `rrsync -ro <dir>`. This
machine pulls; the server has no access to this machine and cannot delete anything
here. One run:

    backup-pull.py pull        # rsync a new snapshot, verify it, prune
    backup-pull.py status      # age of the newest verified snapshot
    backup-pull.py self-test   # verdict parser, pruning, resume and retries; no network

1. `rsync -a --link-dest=<newest verified>` into ONE in-progress directory,
   `.incoming`, renamed to `<UTC stamp>` only when the rsync completed. Unchanged
   files are hard links to the previous snapshot, so a snapshot costs only what
   changed. The in-progress directory survives a failed run, so the next run
   resumes it: files already copied are skipped and a cut file continues from
   `--partial-dir`. Until 2026-10-01 every run wrote a new `.incoming-<stamp>`, so
   a pull cut by the network restarted from scratch; the newest such leftover is
   adopted as `.incoming`, any older one is left in place and named on every run.
   `--delete-after` acts only on `.incoming`, never on a snapshot, so a resumed copy
   loses what the server deleted meanwhile. pgBackRest's in-flight `*.pgbackrest.tmp`
   files are never copied. Within a run, a network-class rsync exit, or 24 (a source
   file vanished mid-copy), is retried up to `RSYNC_ATTEMPTS` times with a growing,
   jittered pause, all inside `RSYNC_TIMEOUT_S`; `--timeout` makes a stalled link fail
   in `RSYNC_IO_TIMEOUT_S` instead of hanging. Any other exit alarms at once.
   A run that reaches `RSYNC_TIMEOUT_S` with an attempt still alive is a pull larger
   than one run, not a dead link (a dead link ends the attempt by `--timeout` first):
   it exits 0, says "pull still in progress", and the next run resumes `.incoming`,
   as long as the newest verified snapshot is younger than `MAX_VERIFIED_AGE_S`.
   Past that age, or with no verified snapshot at all, it alarms as before, so a link
   that trickles forever still surfaces. MEASURED 2026-10-12: during a server-side
   relabel the 01:00 run was cut after 3600 s on attempt 1 and alarmed, and the next
   run resumed and verified at 03:00.
2. `pgbackrest verify --verbose` against the copy, with the repository cipher
   passphrase read from a 0600 file. A snapshot is VERIFIED only when the run
   exits 0 AND its summary says `status: ok` AND nothing in it says `error` or
   `invalid`. MEASURED 2026-09-29 with pgBackRest 2.58: `verify` over a copy with one
   corrupted bundle file printed `status: error` / `checksum invalid: 1` and still
   exited 0, and without `--verbose` it printed no summary at all, so the exit code
   alone would have passed a broken copy. The verdict is written into the snapshot
   (`VERIFIED` or `FAILED`), and `LAST_VERIFIED` names the newest good one.
   Every run verifies the WHOLE repository, every backup set and every WAL file, so
   VERIFIED means the copy as a whole is intact, not only what changed. `verify --set`
   was not taken: MEASURED 2026-10-12 on a 152 GiB copy, `--set` on the newest
   differential checked that set, its full, and 3 of 23,847 WAL files, so the WAL that
   point-in-time recovery needs would go unchecked. Verify runs with
   `--process-max=VERIFY_PROCESS_MAX`; MEASURED 2026-10-12 on the same copy (16 cores,
   load 2 to 5, a live pull rsyncing beside it): 15 min 47 s wall at 1 process, 4 min
   8 s at 4, CPU 913 s and 951 s, identical summaries. It shortens the wall time, not
   the CPU, which is set by the size of the repository.
   A copy taken while the server's own backup is being written holds that backup
   without its manifest, and verify reports it `status: in-progress` and skips it.
   When that is the ONLY reason the copy fails, it is not damage: it is not marked
   FAILED, it goes back to `.incoming`, and the next run resumes it and copies only
   what was missing. It exits 0 while the newest verified snapshot is younger than
   `MAX_VERIFIED_AGE_S`; past that age, or with none, it alarms, so a backup that
   never completes still surfaces. MEASURED 2026-10-20: a copy that began about a
   minute before the nightly backup alarmed "backup status: in" (the parser cut the
   token); the same backup was `valid` in the next night-time copy.
3. Keep the newest `HINDSIGHT_BACKUP_KEEP` verified snapshots (default 3). A failed
   snapshot stays beside them, marked, until it is older than the oldest kept
   verified one. Only directories whose names are a UTC stamp are ever removed.

A failure is reported to the operator's own notification sinks, once per change of
state (ok to failing), not every hour while the laptop is offline; the run still
exits 1 so the unit shows `failed`. Settings come from the gitignored `.env`, so the
unit names nothing: `HINDSIGHT_BACKUP_HOST` (ssh alias), `HINDSIGHT_BACKUP_DIR`,
`HINDSIGHT_BACKUP_CIPHER_FILE`, and optionally `HINDSIGHT_BACKUP_KEEP`,
`HINDSIGHT_BACKUP_STANZA` (default `hindsight`) and `HINDSIGHT_PGBACKREST` (the
binary, default `pgbackrest` on PATH). The pgBackRest here must read the server's
PostgreSQL major version: 2.50 could not read PostgreSQL 18 WAL (MEASURED 2026-09-29,
"unexpected WAL magic").
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.api import load_api_key  # noqa: E402
from scripts.utils.data_path import data_write_dir  # noqa: E402
from scripts.utils.telegram_notify import notify, own_targets  # noqa: E402

STAMP_RE = re.compile(r"^\d{8}T\d{6}Z$")
RSYNC_TIMEOUT_S = 3600       # the whole rsync phase of one run, every attempt and pause
RSYNC_IO_TIMEOUT_S = 120     # rsync --timeout: no data for this long ends the attempt
RSYNC_ATTEMPTS = 5
RETRY_BASE_PAUSE_S = 30      # 30, 60, 120, 240 s, each plus up to a quarter of jitter
RSYNC_TIMED_OUT = "rsync timed out"
# A pull cut by RSYNC_TIMEOUT_S while data still flows is not a failure while the
# newest verified snapshot is younger than this; past it, the cut alarms as before.
MAX_VERIFIED_AGE_S = 6 * 3600
VERIFY_TIMEOUT_S = 3600
VERIFY_PROCESS_MAX = 4
INCOMING = ".incoming"
# Exit codes that mean the link failed, not the transfer. Read 2026-10-01 in rsync(1)
# 3.2.7, EXIT VALUES: 10 socket I/O, 12 protocol data stream (what a broken pipe
# yields), 30 timeout in data send/receive (--timeout), 35 timeout waiting for daemon
# connection. 255 is ssh's own: ssh(1), EXIT STATUS, "255 if an error occurred", and
# rsync passes it through when the remote shell fails to connect. Everything else,
# 23 "partial transfer due to error" included, is not retried.
NETWORK_EXIT_CODES = frozenset({10, 12, 30, 35, 255})
# 24 "partial transfer due to vanished source files" (rsync(1) 3.2.7): the server
# deleted or renamed a listed file before it was sent. The link is fine and the copy is
# merely behind the source, so it is retried like a network exit. MEASURED 2026-10-13:
# an hourly run failed on 24 for a WAL segment pgBackRest renamed mid-copy.
VANISHED_EXIT_CODE = 24
# pgBackRest writes a repository file as `<name>.pgbackrest.tmp` and renames it when
# complete, so such a file is never one the copy needs. `H` hides it on the sending
# side only: it is never listed, and a copy left in `.incoming` by an older run is
# still removed by --delete-after, which a plain exclude would protect.
PGBACKREST_TMP_FILTER = "--filter=H *.pgbackrest.tmp"


class PullError(RuntimeError):
    """The pull or the verification could not be completed."""


def _setting(name: str, default: str | None = None) -> str:
    value = load_api_key(name, required=False) or default
    if not value:
        raise PullError(f"{name} is not set in .env")
    return value


def _stamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _load_verdict():
    """The verify parser lives in the server's status script, which runs the same
    verify on the server's own repository (HOS-180); load it by path (its file name has
    a dash) so the two can never judge one summary differently."""
    path = Path(__file__).resolve().parent / "server" / "hindsight-status.py"
    spec = importlib.util.spec_from_file_location("hindsight_status", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.verify_verdict


verdict = _load_verdict()


def snapshots(root: Path) -> list[tuple[str, str]]:
    """[(stamp, 'VERIFIED'|'FAILED'|'UNMARKED')] oldest first."""
    out = []
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        if d.is_dir() and STAMP_RE.match(d.name):
            mark = next((m for m in ("VERIFIED", "FAILED") if (d / m).exists()), "UNMARKED")
            out.append((d.name, mark))
    return out


def to_prune(snaps: list[tuple[str, str]], keep: int) -> list[str]:
    """Stamps to delete: verified beyond the newest `keep`, and anything else older
    than the oldest verified snapshot that is kept."""
    verified = [s for s, m in snaps if m == "VERIFIED"]
    if not verified:
        return []
    kept = verified[-keep:]
    oldest_kept = kept[0]
    return [s for s, _ in snaps if s not in kept and s < oldest_kept]


def _alarm(root: Path, subject: str, detail: str) -> None:
    print(f"backup-pull: {subject}\n{detail}", file=sys.stderr)
    state = root / ".last-run-state"
    previous = state.read_text(encoding="utf-8").strip() if state.exists() else "ok"
    _write_atomic(state, "failing\n")
    if previous == "failing":
        print("backup-pull: already reported; not notifying again", file=sys.stderr)
        return
    text = f"HEADING OS backup pull: {subject}\n\n{detail}"
    delivered = sum(1 for t in sorted(own_targets()) if notify(t, text))
    if not delivered:
        print("backup-pull: NO NOTIFICATION SENT, no operator sink is configured or the"
              " transport refused.", file=sys.stderr)


def _verify(snap: Path, stanza: str, binary: str, cipher_file: Path) -> tuple[bool, str, str, bool]:
    """(verified, reason, output, busy). `busy` is True only when the copy fails solely
    because a backup in it is in progress, and would verify otherwise."""
    env = dict(os.environ)
    env["PGBACKREST_REPO1_CIPHER_PASS"] = cipher_file.read_text(encoding="utf-8").strip()
    log_dir = Path.home() / ".cache" / "pgbackrest"
    log_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [binary, "--config=/dev/null", f"--stanza={stanza}",
         f"--repo1-path={snap / 'pgbackrest'}", "--repo1-cipher-type=aes-256-cbc",
         f"--log-path={log_dir}", "--log-level-file=off", "--log-level-console=info",
         "--verbose", f"--process-max={VERIFY_PROCESS_MAX}", "verify"],
        capture_output=True, text=True, env=env, timeout=VERIFY_TIMEOUT_S, check=False)
    output = proc.stdout + proc.stderr
    ok, reason = verdict(proc.returncode, output)
    busy = not ok and verdict(proc.returncode, output, in_progress_ok=True)[0]
    return ok, reason, output, busy


def _incoming_dir(root: Path) -> Path:
    """The one in-progress directory, adopting the newest old-scheme leftover."""
    current = root / INCOMING
    old = sorted(d for d in root.glob(f"{INCOMING}-*")
                 if d.is_dir() and STAMP_RE.match(d.name[len(INCOMING) + 1:]))
    if not current.exists() and old:
        adopted = old.pop()
        os.replace(adopted, current)
        print(f"resuming the partial pull {adopted.name} as {INCOMING}")
    for stale in old:
        print(f"note: old partial pull {stale.name} left in place; not used, not removed")
    return current


def _rsync(cmd: list[str], *, deadline_s: float = RSYNC_TIMEOUT_S, sleep=time.sleep,
           clock=time.monotonic, jitter=random.uniform) -> tuple[str, str] | None:
    """Run rsync, retrying network-class and vanished-file exits. None on success, else
    (subject, detail)."""
    start = clock()
    err = ""
    for attempt in range(1, RSYNC_ATTEMPTS + 1):
        remaining = deadline_s - (clock() - start)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=max(remaining, 1), check=False)
        except subprocess.TimeoutExpired:
            return RSYNC_TIMED_OUT, f"after {deadline_s:.0f}s, attempt {attempt}; the partial copy is kept"
        if proc.returncode == 0:
            return None
        err = f"exit {proc.returncode}: {proc.stderr.strip()[-800:]}"
        if proc.returncode == VANISHED_EXIT_CODE:
            why = "a source file vanished mid-copy"
        elif proc.returncode in NETWORK_EXIT_CODES:
            why = "lost the link"
        else:
            return "rsync failed", err
        print(f"backup-pull: attempt {attempt}/{RSYNC_ATTEMPTS} {why}, {err}", file=sys.stderr)
        if attempt == RSYNC_ATTEMPTS:
            break
        base = RETRY_BASE_PAUSE_S * 2 ** (attempt - 1)
        pause = base + jitter(0, base / 4)
        if clock() - start + pause >= deadline_s:
            return "rsync failed", (f"{err}\ngave up after {attempt} attempt(s): the next pause would pass"
                                    f" {deadline_s:.0f}s; the partial copy is kept for the next run")
        sleep(pause)
    return "rsync failed", (f"{err}\nrsync failed on all {RSYNC_ATTEMPTS} attempts; the partial copy"
                            " is kept and the next run resumes it")


def pull(root: Path, host: str, cipher_file: Path, keep: int, stanza: str, binary: str, *,
         alarm=None, **rsync_opts) -> int:
    alarm = alarm or _alarm
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = _stamp(datetime.now(timezone.utc))
    incoming = _incoming_dir(root)
    verified = [s for s, m in snapshots(root) if m == "VERIFIED"]
    cmd = ["rsync", "-a", "--delete-after", f"--timeout={RSYNC_IO_TIMEOUT_S}",
           "--partial-dir=.rsync-partial", PGBACKREST_TMP_FILTER, f"{host}:", f"{incoming}/"]
    if verified:
        cmd[2:2] = [f"--link-dest={root / verified[-1]}"]
    failed = _rsync(cmd, **rsync_opts)
    if failed and failed[0] == RSYNC_TIMED_OUT and verified:
        # rsync --timeout ends an attempt after RSYNC_IO_TIMEOUT_S with no data, so an
        # attempt still alive at the deadline moved data within that window: the link
        # is up and the volume is larger than one run. The kept `.incoming` resumes.
        newest = datetime.strptime(verified[-1], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        age_s = (datetime.now(timezone.utc) - newest).total_seconds()
        if age_s < MAX_VERIFIED_AGE_S:
            print(f"pull still in progress: {failed[1]}; newest verified {verified[-1]} is"
                  f" {age_s / 3600:.1f} h old, the next run resumes")
            return 0
        failed = (failed[0], f"{failed[1]}\nnewest verified {verified[-1]} is {age_s / 3600:.1f} h old")
    if failed:
        alarm(root, *failed)
        return 1
    snap = root / stamp
    os.replace(incoming, snap)

    try:
        ok, reason, output, busy = _verify(snap, stanza, binary, cipher_file)
    except (OSError, subprocess.TimeoutExpired) as exc:
        ok, reason, output, busy = False, f"verify could not run: {exc}", "", False
    if busy:
        # The copy caught the server's backup mid-write: verify skipped that backup and
        # passed everything else. Not damage, so it is not marked FAILED; it goes back
        # to `.incoming`, and the next run resumes it and copies only the rest.
        os.replace(snap, incoming)
        newest = (datetime.strptime(verified[-1], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                  if verified else None)
        age_s = (datetime.now(timezone.utc) - newest).total_seconds() if newest else None
        if age_s is not None and age_s < MAX_VERIFIED_AGE_S:
            print(f"copy caught a backup in progress ({reason}); kept as {INCOMING}, newest verified"
                  f" {verified[-1]} is {age_s / 3600:.1f} h old, the next run completes it")
            return 0
        held = (f"newest verified {verified[-1]} is {age_s / 3600:.1f} h old" if newest
                else "there is no verified snapshot")
        alarm(root, "snapshot failed verification",
              f"{stamp}: {reason}\n{held}; the copy is kept as {INCOMING} for the next run")
        return 1
    _write_atomic(snap / ("VERIFIED" if ok else "FAILED"), f"{reason}\n\n{output}")
    if not ok:
        alarm(root, "snapshot failed verification", f"{stamp}: {reason}")
        return 1
    _write_atomic(root / "LAST_VERIFIED", stamp + "\n")
    _write_atomic(root / ".last-run-state", "ok\n")

    for old in to_prune(snapshots(root), keep):
        shutil.rmtree(root / old)
        print(f"pruned {old}")
    print(f"verified snapshot {stamp}; kept {min(len(verified) + 1, keep)} verified")
    return 0


def cmd_pull(_args) -> int:
    host = _setting("HINDSIGHT_BACKUP_HOST")
    root = Path(_setting("HINDSIGHT_BACKUP_DIR")).expanduser()
    cipher_file = Path(_setting("HINDSIGHT_BACKUP_CIPHER_FILE")).expanduser()
    keep = int(_setting("HINDSIGHT_BACKUP_KEEP", "3"))
    stanza = _setting("HINDSIGHT_BACKUP_STANZA", "hindsight")
    binary = _setting("HINDSIGHT_PGBACKREST", "pgbackrest")
    if keep < 1:
        raise PullError("HINDSIGHT_BACKUP_KEEP must be at least 1")
    return pull(root, host, cipher_file, keep, stanza, binary)


def cmd_status(_args) -> int:
    root = Path(_setting("HINDSIGHT_BACKUP_DIR")).expanduser()
    last = root / "LAST_VERIFIED"
    if not last.exists():
        print("no verified snapshot yet")
        return 1
    stamp = last.read_text(encoding="utf-8").strip()
    taken = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    hours = (datetime.now(timezone.utc) - taken).total_seconds() / 3600
    print(f"newest verified snapshot {stamp}, {hours:.1f} h old")
    for s, m in snapshots(root):
        print(f"  {s}  {m}")
    return 0


_FAKE_RSYNC = """#!/bin/bash
# Stand-in for rsync in the self-test: exit code from the plan file, one line per call.
plan="$BACKUP_PULL_FAKE_PLAN"
code=$(head -n1 "$plan"); sed -i 1d "$plan"; code=${code:-99}
timeout=no
for a in "$@"; do dest="$a"; case "$a" in --timeout=*) timeout=yes;; esac; done
echo "$dest $timeout" >> "$plan.log"
mkdir -p "$dest/pgbackrest"
echo x >> "$dest/pgbackrest/copied"
[ "$code" != 0 ] && echo "fake rsync: Broken pipe" >&2
exit "$code"
"""


def _pull_scenarios(ok_out: str, bad_out: str, busy_out: str) -> list[tuple[str, bool]]:
    """End-to-end pull() against a fake rsync and pgbackrest on PATH. No network, no
    notification: the alarm is a recorder and the clock and sleep are fakes."""
    cases: list[tuple[str, bool]] = []
    with tempfile.TemporaryDirectory(prefix="backup-pull-st.",
                                     dir=data_write_dir("runtime:scratch/tmp")) as td:
        tmp = Path(td)
        fake_bin = tmp / "bin"
        fake_bin.mkdir()
        scripts = {"rsync": _FAKE_RSYNC}
        for name, out in (("ok", ok_out), ("bad", bad_out), ("busy", busy_out)):
            (fake_bin / f"{name}.out").write_text(out, encoding="utf-8")
            scripts[f"pgbackrest-{name}"] = f'#!/bin/bash\ncat "$(dirname "$0")/{name}.out"\n'
        for name, text in scripts.items():
            (fake_bin / name).write_text(text, encoding="utf-8")
            (fake_bin / name).chmod(0o700)
        cipher = tmp / "cipher"
        cipher.write_text("invented-passphrase\n", encoding="utf-8")
        saved = {k: os.environ.get(k) for k in ("PATH", "BACKUP_PULL_FAKE_PLAN")}
        os.environ["PATH"] = f"{fake_bin}{os.pathsep}{saved['PATH'] or ''}"

        def run(root: Path, codes: list[int], verify: str = "ok", deadline: float = RSYNC_TIMEOUT_S):
            plan = tmp / "plan"
            plan.write_text("".join(f"{c}\n" for c in codes), encoding="utf-8")
            log = tmp / "plan.log"
            log.unlink(missing_ok=True)
            os.environ["BACKUP_PULL_FAKE_PLAN"] = str(plan)
            alarms: list[str] = []
            pauses: list[float] = []
            now = [0.0]

            def sleep(seconds: float) -> None:
                pauses.append(seconds)
                now[0] += seconds

            rc = pull(root, "invented-host", cipher, 3, "demo", str(fake_bin / f"pgbackrest-{verify}"),
                      alarm=lambda _root, subject, _detail: alarms.append(subject),
                      deadline_s=deadline, sleep=sleep, clock=lambda: now[0], jitter=lambda _a, b: b)
            calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
            return rc, alarms, pauses, calls

        def partials(root: Path) -> list[str]:
            return sorted(d.name for d in root.iterdir() if d.name.startswith(INCOMING))

        try:
            root = tmp / "cut"
            rc, alarms, pauses, calls = run(root, [12] * RSYNC_ATTEMPTS)
            first_dest = calls[0].split()[0] if calls else ""
            cases += [
                ("link lost on every attempt: 5 attempts, one alarm, exit 1",
                 rc == 1 and len(calls) == RSYNC_ATTEMPTS and alarms == ["rsync failed"]),
                ("pauses grow between attempts", pauses == [37.5, 75.0, 150.0, 300.0]),
                ("after the failure exactly one in-progress directory remains", partials(root) == [INCOMING]),
                ("every attempt passes rsync --timeout", all(c.endswith(" yes") for c in calls)),
            ]
            rc, alarms, _, calls = run(root, [0])
            snaps = snapshots(root)
            copied = root / snaps[0][0] / "pgbackrest" / "copied" if snaps else tmp / "none"
            cases += [
                ("the next run continues the same directory",
                 rc == 0 and calls and calls[0].split()[0] == first_dest == f"{root / INCOMING}/"),
                ("the resumed copy keeps what the cut run fetched",
                 copied.exists() and len(copied.read_text().splitlines()) == RSYNC_ATTEMPTS + 1),
                ("a completed resume is renamed and VERIFIED, nothing in progress left",
                 [m for _, m in snaps] == ["VERIFIED"] and partials(root) == [] and not alarms),
            ]

            root = tmp / "retry"
            rc, alarms, pauses, calls = run(root, [255, 30, 0])
            cases.append(("a retry succeeds on attempt 3 with no alarm",
                          rc == 0 and len(calls) == 3 and not alarms and pauses == [37.5, 75.0]
                          and [m for _, m in snapshots(root)] == ["VERIFIED"]))

            root = tmp / "hard"
            rc, alarms, pauses, calls = run(root, [23, 0])
            cases.append(("a non-network exit alarms at once with no retry",
                          rc == 1 and len(calls) == 1 and not pauses and alarms == ["rsync failed"]
                          and partials(root) == [INCOMING]))

            root = tmp / "deadline"
            rc, alarms, pauses, calls = run(root, [12] * RSYNC_ATTEMPTS, deadline=100)
            cases.append(("retries stop before a pause would pass the overall limit",
                          rc == 1 and len(calls) == 2 and pauses == [37.5] and alarms == ["rsync failed"]
                          and partials(root) == [INCOMING]))

            root = tmp / "adopt"
            for stamp, marker in (("20260101T000000Z", "older"), ("20260102T000000Z", "newer")):
                (root / f"{INCOMING}-{stamp}").mkdir(parents=True)
                (root / f"{INCOMING}-{stamp}" / marker).write_text("x", encoding="utf-8")
            rc, alarms, _, calls = run(root, [0])
            snaps = snapshots(root)
            cases += [
                ("the newest old-scheme leftover is adopted and resumed",
                 rc == 0 and len(snaps) == 1 and (root / snaps[0][0] / "newer").exists()),
                ("an older old-scheme leftover is left untouched",
                 (root / f"{INCOMING}-20260101T000000Z" / "older").exists()
                 and partials(root) == [f"{INCOMING}-20260101T000000Z"]),
            ]

            root = tmp / "badverify"
            rc, alarms, _, _ = run(root, [0], verify="bad")
            cases.append(("a copy that fails verify is marked FAILED and alarms",
                          rc == 1 and [m for _, m in snapshots(root)] == ["FAILED"]
                          and alarms == ["snapshot failed verification"]
                          and not (root / "LAST_VERIFIED").exists()))

            def verified_ago(root: Path, hours: float) -> None:
                snap = root / _stamp(datetime.now(timezone.utc) - timedelta(hours=hours))
                snap.mkdir(parents=True)
                (snap / "VERIFIED").write_text("invented\n", encoding="utf-8")

            root = tmp / "busy"
            verified_ago(root, 1)
            rc, alarms, _, _ = run(root, [0], verify="busy")
            cases.append(("a copy holding a backup in progress is neither FAILED nor alarmed",
                          rc == 0 and not alarms and [m for _, m in snapshots(root)] == ["VERIFIED"]
                          and partials(root) == [INCOMING]))
            rc, alarms, _, calls = run(root, [0])
            cases.append(("the next run resumes that copy and verifies it",
                          rc == 0 and not alarms and calls and calls[0].split()[0] == f"{root / INCOMING}/"
                          and [m for _, m in snapshots(root)] == ["VERIFIED", "VERIFIED"]
                          and partials(root) == []))

            root = tmp / "busy-stale"
            verified_ago(root, MAX_VERIFIED_AGE_S / 3600 + 1)
            rc, alarms, _, _ = run(root, [0], verify="busy")
            cases.append(("an in-progress copy alarms once the newest verified is too old, still not FAILED",
                          rc == 1 and alarms == ["snapshot failed verification"]
                          and [m for _, m in snapshots(root)] == ["VERIFIED"] and partials(root) == [INCOMING]))
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    return cases


def cmd_self_test(_args) -> int:
    ok_out = ("status: ok\n archiveId: 18-1\n missing: 0, checksum invalid: 0, size invalid: 0, other: 0\n"
              " backup: 20260101-000000F, status: valid\n missing: 0, checksum invalid: 0, size invalid: 0, other: 0\n")
    bad_out = ("status: error\n missing: 0, checksum invalid: 0, size invalid: 0, other: 0\n"
               " backup: 20260101-000000F, status: invalid\n missing: 0, checksum invalid: 1, size invalid: 0, other: 0\n")
    busy_out = ok_out + (" backup: 20260102-000000F_20260102-010000D, status: in-progress,"
                         " total files checked: 0, total valid files: 0\n")
    cases = [
        ("clean summary verifies", verdict(0, ok_out)[0] is True),
        ("error summary with exit 0 is refused", verdict(0, bad_out)[0] is False),
        ("non-zero exit is refused", verdict(46, ok_out)[0] is False),
        ("no summary (no --verbose) is refused", verdict(0, "verify command end: completed successfully")[0] is False),
        ("invalid backup under an ok stanza is refused",
         verdict(0, ok_out.replace("status: valid", "status: invalid"))[0] is False),
        ("an in-progress backup is refused and named whole", verdict(0, busy_out) == (False, "backup status: in-progress")),
        ("an in-progress backup passes only when allowed", verdict(0, busy_out, in_progress_ok=True)[0] is True),
        ("in progress beside an invalid backup is refused even when allowed",
         verdict(0, busy_out.replace("status: valid", "status: invalid"), in_progress_ok=True)[0] is False),
        ("a missing manifest is refused and named whole",
         verdict(0, busy_out.replace("in-progress", "manifest missing"), in_progress_ok=True)
         == (False, "backup status: manifest missing")),
    ]
    v = [(f"2026010{i}T000000Z", "VERIFIED") for i in range(1, 6)]
    snaps = sorted(v + [("20260102T120000Z", "FAILED"), ("20260105T120000Z", "FAILED")])
    pruned = to_prune(snaps, 3)
    cases += [
        ("keeps the newest 3 verified, drops older ones and older failures",
         pruned == ["20260101T000000Z", "20260102T000000Z", "20260102T120000Z"]),
        ("a failure newer than the oldest kept stays", "20260105T120000Z" not in pruned),
        ("nothing pruned without a verified snapshot", to_prune([("20260101T000000Z", "FAILED")], 3) == []),
    ]
    cases += _pull_scenarios(ok_out, bad_out, busy_out)
    failures = 0
    for label, ok in cases:
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {label}")
    print(f"{len(cases) - failures}/{len(cases)} passed")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pull").set_defaults(fn=cmd_pull)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("self-test").set_defaults(fn=cmd_self_test)
    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except (PullError, ValueError, OSError) as exc:
        print(f"backup-pull: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
