#!/usr/bin/env python3
"""Read-only health snapshot of a Hindsight server, printed as one JSON object.

Installed on the server as /usr/local/sbin/hindsight-status and reached only through
an ssh forced command (`sudo -n /usr/local/sbin/hindsight-status`) for a dedicated
monitoring user, so the laptop's health check needs no admin access. It changes
nothing. Standard library only.

Fields: services (systemd state per unit), halt (cliproxy-update HALT text or null),
proxy_events (cliproxy-update events newer than `--since`, ISO 8601), cert_not_after,
disk_pct (root filesystem), queue_waiting (pending + processing async operations),
queue_last_done_min (minutes since the newest operation COMPLETED, null when none ever
has), queue_oldest_min (oldest pending/processing operation; kept for readers of the
old field, no longer judged on its own), llm_auth_errors (LLM trace rows in the last
hour whose error mentions 401/403), ops_failed (async operations now in status
`failed`, per operation type: count and the newest failure's time; {} when none),
backup_newest (newest pgBackRest backup label and stop time), backup_status,
verify_result (systemd Result of the weekly verify unit).

A value that could not be read is null together with an entry in `errors`, never a
guess, so the reader can tell "fine" from "unknown".

    hindsight-status [--since ISO]      # print the snapshot as JSON
    hindsight-status --pulse [--dry-run] # judge it and ping Healthchecks.io (timer, root)
    hindsight-status --verify-backup     # pgbackrest verify, judged by its summary (weekly unit)
    hindsight-status --self-test         # the judging rules on invented input; changes nothing

`--pulse` is the external dead-man signal (operator decision, 2026-09-30: an outside pulse that
notices a dead server while the laptop is off). It reads the ping URL from
/etc/hindsight/pulse-url; the alert itself comes from Healthchecks.io when pings stop.

The queue is judged by MOVEMENT, not by the age of its oldest item (HOS-179). MEASURED
2026-09-30: judging the oldest pending item sent `/fail` 101 of 139 pulses in 12 h
during a bulk load; MEASURED 2026-10-01 on the server: 2593 completed (the newest 0 min
before), 81 pending, 25 processing. A long queue that is being worked is healthy; the
problem is work waiting while nothing completes.

`--verify-backup` exists because `pgbackrest verify` exits 0 on a corrupted repository
(MEASURED 2026-09-29 with pgBackRest 2.58: one corrupted file gave `status: error` in
the `--verbose` summary and exit 0; without `--verbose` there is no summary). It exits
non-zero unless the summary says ok, and an unparseable summary is not ok (HOS-180).
`verify_verdict` is the one parser; the laptop's backup-pull.py loads it from here.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

UNITS = ["postgresql@18-main", "cliproxyapi", "hindsight-api", "hindsight-worker", "hindsight-cp", "nginx"]
HALT = Path("/var/lib/cliproxy-update/HALT")
EVENTS = Path("/var/lib/cliproxy-update/events.jsonl")
SETTINGS = Path("/etc/default/hindsight-status")  # CERT=<path to the served fullchain.pem>


CERT_MIN_DAYS = 20
DISK_MAX_PCT = 75
# Work waiting and nothing completed for this long is a stall. 30 is HELM's starting
# proposal of 2026-10-01 (the old oldest-item limit carried over), not a measurement.
QUEUE_STALL_MIN = 30
VERIFY_UNIT = "pgbackrest-verify.service"
VERIFY_TIMEOUT_S = 3600
STANZA = "hindsight"
PULSE_URL_FILE = Path("/etc/hindsight/pulse-url")  # Healthchecks.io ping URL, 0600 root


def problems(snap: dict, now: datetime) -> list[str]:
    """Problems from one snapshot (pure). Shared with the laptop's hindsight-health.py,
    which loads this file by path, so the server's own pulse and the laptop's hourly
    check judge by the same rules."""
    out = []
    for unit, state in sorted(snap.get("services", {}).items()):
        if state != "active":
            out.append(f"service {unit} is {state}")
    if snap.get("halt"):
        out.append(f"CLIProxyAPI updates halted: {snap['halt']}")
    na = snap.get("cert_not_after")
    if na and datetime.fromisoformat(na) - now < timedelta(days=CERT_MIN_DAYS):
        out.append(f"certificate on the server expires {na[:10]} (renewal did not arrive)")
    if (snap.get("disk_pct") or 0) > DISK_MAX_PCT:
        out.append(f"server disk {snap['disk_pct']} % full")
    stall = queue_stall(snap)
    if stall:
        out.append(stall)
    if (snap.get("llm_auth_errors") or 0) > 0:
        out.append(f"{snap['llm_auth_errors']} LLM request(s) refused with 401/403 in the last hour")
    failed = ops_failed(snap)
    if failed:
        out.append(failed)
    if snap.get("backup_status") not in (None, "ok"):
        out.append(f"server backup status: {snap['backup_status']}")
    if snap.get("verify_result") not in (None, "success"):
        out.append(f"weekly backup verify failed: {snap['verify_result']}")
    for err in snap.get("errors", []):
        out.append(f"server could not read: {err}")
    return out


def queue_stall(snap: dict) -> str | None:
    """The queue problem, or None. Waiting work with no completion for QUEUE_STALL_MIN
    is a stall; an empty queue never is. When nothing has EVER completed, the age of
    the oldest waiting item is how long the queue has gone without progress. A snapshot
    from a status script older than HOS-179 carries no `queue_waiting` and is judged by
    the old oldest-item rule, so a laptop ahead of its server still sees a stall."""
    if "queue_waiting" not in snap:
        oldest = snap.get("queue_oldest_min") or 0
        return f"oldest queued Hindsight operation is {oldest} min old" if oldest > QUEUE_STALL_MIN else None
    if not snap["queue_waiting"]:
        return None
    done = snap.get("queue_last_done_min")
    idle = done if done is not None else snap.get("queue_oldest_min")
    if idle is None or idle <= QUEUE_STALL_MIN:
        return None
    since = "no operation has ever completed" if done is None else f"nothing completed for {idle} min"
    return f"Hindsight queue stalled: {snap['queue_waiting']} operation(s) waiting, {since}"


def ops_failed(snap: dict) -> str | None:
    """The failed-operations problem, or None. Any failed operation is a problem, with no
    threshold, as any LLM 401/403 is: nothing in Hindsight retries one by itself, so it
    stays failed until someone retries or deletes it (HOS-253; MEASURED 2026-10-23: five
    `refresh_mental_model` operations failed 2026-10-14/15 and sat unseen for nine days).
    A snapshot without the field comes from a status script older than HOS-253 and is
    not judged on it."""
    failed = snap.get("ops_failed") or {}
    total = sum(v["count"] for v in failed.values())
    if not total:
        return None
    parts = [f"{t} {v['count']}" + (f", newest {v['newest']}" if v.get("newest") else "")
             for t, v in sorted(failed.items())]
    return f"Hindsight operations failed: {total} ({'; '.join(parts)})"


# The verify summary's status tokens, read 2026-10-20 in pgBackRest's own source,
# src/command/verify/verify.c, at tag release/2.58.0 and on main (last change to the file
# 2026-10-02), functions verifyRender and verifyProcess: the stanza line is
# "status: ok" or "status: error"; each backup line is ", status: <s>, total files
# checked" with <s> one of "valid", "invalid", "manifest missing" or "in-progress".
# Two of those carry a space or a dash, so a token runs to the next comma or newline:
# `\w+` cut "in-progress" to "in" (MEASURED 2026-10-20, the alarm "backup status: in").
# "in-progress" means the newest label had no backup.manifest yet, so verify SKIPPED it:
# pgBackRest prints it only under --verbose and does not count it as an error.
IN_PROGRESS = "in-progress"


def verify_verdict(returncode: int, output: str, *, in_progress_ok: bool = False) -> tuple[bool, str]:
    """(verified, reason) from one `pgbackrest verify --verbose` run. Verified only when
    the run exits 0 AND the summary's stanza status is ok AND every backup is valid AND
    no file is counted invalid, missing or unreadable. `in_progress_ok` lets a backup
    skipped as in progress pass; everything else is judged the same."""
    if returncode != 0:
        return False, f"pgbackrest verify exited {returncode}"
    statuses = [s.strip() for s in re.findall(r"\bstatus:[ \t]*([^,\n]+)", output)]
    if not statuses:
        return False, "no status line in the verify summary (was --verbose passed?)"
    if statuses[0] != "ok":
        return False, f"stanza status: {statuses[0]}"
    allowed = ("valid", "ok", IN_PROGRESS) if in_progress_ok else ("valid", "ok")
    bad = [s for s in statuses[1:] if s not in allowed]
    if bad:
        return False, f"backup status: {', '.join(bad)}"
    if re.search(r"checksum invalid: [1-9]|missing: [1-9]|size invalid: [1-9]|other: [1-9]", output):
        return False, "summary counts an invalid, missing or unreadable file"
    return True, "status: ok"


def verify_backup(runner=subprocess.run) -> int:
    """Run the server's weekly verify and exit by its SUMMARY, not its exit code. The
    unit runs this as `postgres`; run as root it drops to `postgres` the way every
    other pgBackRest call here does. `--log-level-console=info` is the measured
    invocation that prints the summary (backup-pull.py, 2026-09-29)."""
    cmd = ["pgbackrest", f"--stanza={STANZA}", "--log-level-console=info", "--verbose", "verify"]
    if os.geteuid() == 0:
        cmd = ["sudo", "-u", "postgres", *cmd]
    try:
        proc = runner(cmd, capture_output=True, text=True, timeout=VERIFY_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"verify FAILED: could not run: {exc}")
        return 1
    output = proc.stdout + proc.stderr
    ok, reason = verify_verdict(proc.returncode, output)
    print(output.rstrip())
    print(f"verify {'ok' if ok else 'FAILED'}: {reason}")
    return 0 if ok else 1


def unit_result(text: str) -> str | None:
    """systemd Result of a unit from `systemctl show -p LoadState -p Result` output.
    MEASURED 2026-10-01 on systemd 255: a unit that does not exist reports
    `Result=success` beside `LoadState=not-found`, so the load state is checked first."""
    props = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    if props.get("LoadState") != "loaded":
        raise ValueError(f"{VERIFY_UNIT} is {props.get('LoadState', 'unknown')}")
    return props.get("Result") or None


def pulse(snap: dict, url_file: Path, dry_run: bool) -> int:
    """Tell Healthchecks.io the server is alive: a plain ping when healthy, `/fail` with
    the problem list otherwise. Silence is the alarm for a dead server, so this runs from
    a timer on the server itself and needs nothing from the laptop. Only problem text
    leaves the machine, never stored content."""
    found = problems(snap, datetime.now(timezone.utc))
    url = url_file.read_text(encoding="utf-8").strip()
    if not url.startswith("https://"):
        raise ValueError(f"{url_file} must hold an https ping URL")
    target = url + ("/fail" if found else "")
    body = ("\n".join(found) if found else "healthy").encode()
    print(f"pulse {'FAIL' if found else 'ok'}: {len(found)} problem(s)" + (" (dry run, not sent)" if dry_run else ""))
    for p in found:
        print(f"  {p}")
    if dry_run:
        return 0
    req = urllib.request.Request(target, data=body, method="POST")  # noqa: S310 - https checked above
    with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 - https checked above
        return 0 if r.status == 200 else 1


def cert_path() -> Path | None:
    if SETTINGS.exists():
        for line in SETTINGS.read_text(encoding="utf-8").splitlines():
            if line.startswith("CERT="):
                return Path(line.split("=", 1)[1].strip())
    return None


def run(cmd: list[str], **kw) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30, **kw).stdout.strip()


def psql(sql: str) -> str:
    return run(["sudo", "-u", "postgres", "psql", "-d", "hindsight", "-At", "-c", sql])


VERIFY_OK = ("status: ok\n archiveId: 18-1\n missing: 0, checksum invalid: 0, size invalid: 0, other: 0\n"
             " backup: 20260101-000000F, status: valid\n missing: 0, checksum invalid: 0, size invalid: 0, other: 0\n")
VERIFY_BAD = ("status: error\n missing: 0, checksum invalid: 0, size invalid: 0, other: 0\n"
              " backup: 20260101-000000F, status: invalid\n missing: 0, checksum invalid: 1, size invalid: 0, other: 0\n")


def self_test() -> int:
    """The judging rules on invented snapshots and invented verify summaries. Runs no
    command, reads nothing from the machine, sends nothing."""
    import contextlib
    import io

    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    good = {"services": {"a": "active"}, "halt": None, "cert_not_after": "2026-12-28T14:06:55+00:00",
            "disk_pct": 4.0, "queue_waiting": 0, "queue_oldest_min": 0.0, "queue_last_done_min": 3.0,
            "llm_auth_errors": 0, "backup_status": "ok", "verify_result": "success", "errors": []}

    def n(**kw) -> int:
        return len(problems({**good, **kw}, now))

    def fake(rc: int, out: str):
        def runner(cmd, **_kw):
            return subprocess.CompletedProcess(cmd, rc, out, "")
        return runner

    def broken(cmd, **_kw):
        raise FileNotFoundError("pgbackrest")

    def verify(runner) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return verify_backup(runner)

    def unit_raises(text: str) -> bool:
        try:
            unit_result(text)
        except ValueError:
            return True
        return False

    cases = [
        ("healthy snapshot raises nothing", n() == 0),
        ("long queue that is moving raises nothing",
         n(queue_waiting=106, queue_oldest_min=300.0, queue_last_done_min=0.0) == 0),
        ("waiting work with nothing completed for 45 min raised",
         n(queue_waiting=5, queue_oldest_min=50.0, queue_last_done_min=45.0) == 1),
        ("empty queue raises nothing however long since the last completion",
         n(queue_waiting=0, queue_oldest_min=0.0, queue_last_done_min=900.0) == 0),
        ("waiting work and nothing ever completed, oldest 45 min, raised",
         n(queue_waiting=2, queue_oldest_min=45.0, queue_last_done_min=None) == 1),
        ("waiting work and nothing ever completed, oldest 10 min, raises nothing",
         n(queue_waiting=2, queue_oldest_min=10.0, queue_last_done_min=None) == 0),
        ("snapshot from the old status script still judged by oldest item",
         len(problems({k: v for k, v in good.items() if k not in ("queue_waiting", "queue_last_done_min")}
                      | {"queue_oldest_min": 45.0}, now)) == 1),
        ("unreadable queue reported once, through errors",
         n(queue_waiting=None, queue_oldest_min=None, queue_last_done_min=None, errors=["queue: x"]) == 1),
        ("failed weekly verify raised", n(verify_result="exit-code") == 1),
        ("verify unit never run yet raises nothing", n(verify_result=None) == 0),
        ("clean summary verifies", verify_verdict(0, VERIFY_OK)[0] is True),
        ("error summary with exit 0 is refused", verify_verdict(0, VERIFY_BAD)[0] is False),
        ("non-zero exit is refused", verify_verdict(46, VERIFY_OK)[0] is False),
        ("no summary is refused", verify_verdict(0, "verify command end: completed successfully")[0] is False),
        ("error stanza with valid backups and zero counts is refused",
         verify_verdict(0, VERIFY_OK.replace("status: ok", "status: error", 1))[0] is False),
        ("invalid backup under an ok stanza is refused",
         verify_verdict(0, VERIFY_OK.replace("status: valid", "status: invalid"))[0] is False),
        ("--verify-backup exits 1 on status: error with exit 0", verify(fake(0, VERIFY_BAD)) == 1),
        ("--verify-backup exits 0 on a clean summary", verify(fake(0, VERIFY_OK)) == 0),
        ("--verify-backup exits 1 when no summary is printed", verify(fake(0, "completed successfully")) == 1),
        ("--verify-backup exits 1 when pgbackrest cannot run", verify(broken) == 1),
        ("unit Result read when loaded", unit_result("Result=exit-code\nLoadState=loaded\n") == "exit-code"),
        ("missing verify unit is an error, not success", unit_raises("Result=success\nLoadState=not-found\n")),
    ]
    failures = 0
    for label, ok in cases:
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {label}")
    print(f"{len(cases) - failures}/{len(cases)} passed")
    return 1 if failures else 0


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        return self_test()
    if "--verify-backup" in argv:
        return verify_backup()
    since = argv[argv.index("--since") + 1] if "--since" in argv else "1970-01-01T00:00:00+00:00"
    out: dict = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "errors": []}

    out["services"] = {}
    for u in UNITS:
        r = subprocess.run(["systemctl", "is-active", u], capture_output=True, text=True, check=False)
        out["services"][u] = r.stdout.strip() or "unknown"

    out["halt"] = HALT.read_text(encoding="utf-8").strip() if HALT.exists() else None
    out["proxy_events"] = []
    if EVENTS.exists():
        for line in EVENTS.read_text(encoding="utf-8").splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                out["errors"].append("unparseable line in events.jsonl")
                continue
            if ev.get("ts", "") > since and ev.get("event") in ("updated", "update_failed", "rolled_back",
                                                                  "halt", "blacklisted", "download_refused", "error"):
                out["proxy_events"].append(ev)

    try:
        cert = cert_path()
        if cert is None:
            raise ValueError(f"CERT not set in {SETTINGS}")
        end = run(["openssl", "x509", "-in", str(cert), "-noout", "-enddate"]).split("=", 1)[1]
        out["cert_not_after"] = datetime.strptime(end, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc).isoformat()
    except (subprocess.SubprocessError, OSError, ValueError, IndexError) as exc:
        out["cert_not_after"] = None
        out["errors"].append(f"cert: {exc}")

    du = shutil.disk_usage("/")
    out["disk_pct"] = round(100 * du.used / du.total, 1)

    try:
        # completed_at is set on every completion path (read 2026-10-01 in the Hindsight
        # source, memory_engine.py and worker/poller.py at f2f24887c); updated_at is the
        # fallback for a completed row written by a version that did not set it.
        v = psql("select count(*) filter (where status in ('pending','processing')), "
                 "coalesce(extract(epoch from now() - min(created_at) filter "
                 "(where status in ('pending','processing')))/60, 0), "
                 "extract(epoch from now() - max(coalesce(completed_at, updated_at)) filter "
                 "(where status = 'completed'))/60 from async_operations")
        waiting, oldest, done = v.split("|")
        out["queue_waiting"] = int(waiting)
        out["queue_oldest_min"] = round(float(oldest or 0), 1)
        out["queue_last_done_min"] = round(float(done), 1) if done else None
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        out["queue_waiting"] = out["queue_oldest_min"] = out["queue_last_done_min"] = None
        out["errors"].append(f"queue: {exc}")
    try:
        v = psql("select count(*) from llm_requests where started_at > now() - interval '1 hour' "
                 "and status <> 'success' and (error::text ~ '\\m(401|403)\\M')")
        out["llm_auth_errors"] = int(v or 0)
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        out["llm_auth_errors"] = None
        out["errors"].append(f"llm: {exc}")
    try:
        # Contract read 2026-10-23 in the Hindsight 1.1.0 source (hindsight-api-slim):
        # async_operations has operation_type (text) and status; a failure sets
        # status='failed' and updated_at = NOW(), and completed_at only on some paths
        # (engine/memory_engine.py, the UPDATEs near lines 5599 and 5657), so the time of
        # a failure is coalesce(completed_at, updated_at). retry_operation sets the same
        # row back to 'pending', so a retried failure leaves this count. Read-only.
        v = psql("select operation_type, count(*), to_char(max(coalesce(completed_at, updated_at)) "
                 "at time zone 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"+00:00\"') "
                 "from async_operations where status = 'failed' group by operation_type")
        failed = {}
        for line in v.splitlines():
            op_type, count, newest = line.rsplit("|", 2)
            failed[op_type] = {"count": int(count), "newest": newest or None}
        out["ops_failed"] = failed
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        out["ops_failed"] = None
        out["errors"].append(f"failed operations: {exc}")
    try:
        info = json.loads(run(["sudo", "-u", "postgres", "pgbackrest", "--stanza=hindsight", "--output=json", "info"]))
        backups = info[0].get("backup", [])
        last = backups[-1] if backups else None
        out["backup_newest"] = {"label": last["label"], "stop": datetime.fromtimestamp(
            last["timestamp"]["stop"], timezone.utc).isoformat()} if last else None
        out["backup_status"] = info[0].get("status", {}).get("message")
    except (subprocess.SubprocessError, OSError, ValueError, KeyError, IndexError) as exc:
        out["backup_newest"] = None
        out["errors"].append(f"pgbackrest: {exc}")

    try:
        out["verify_result"] = unit_result(run(["systemctl", "show", "-p", "LoadState", "-p", "Result", VERIFY_UNIT]))
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        out["verify_result"] = None
        out["errors"].append(f"verify unit: {exc}")

    if "--pulse" in argv:
        return pulse(out, PULSE_URL_FILE, dry_run="--dry-run" in argv)
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
