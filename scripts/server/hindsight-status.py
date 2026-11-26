#!/usr/bin/env python3
"""Read-only health snapshot of a Hindsight server, printed as one JSON object.

Installed on the server as /usr/local/sbin/hindsight-status and reached only through
an ssh forced command (`sudo -n /usr/local/sbin/hindsight-status`) for a dedicated
monitoring user, so the laptop's health check needs no admin access. It changes
nothing. Standard library only.

Fields: services (systemd state per unit), halt (cliproxy-update HALT text or null),
auth_expired (cliproxy-update AUTH_EXPIRED text, `<provider> <UTC time> <message>`, or
null: present while a subscription login of the proxy is expired, removed by
cliproxy-update once its checks pass), proxy_events (cliproxy-update events newer than `--since`, ISO 8601), cert_not_after,
disk_pct (root filesystem), queue_waiting (pending async operations that are due, plus processing ones),
queue_last_done_min (minutes since the newest operation COMPLETED, null when none ever
has), queue_oldest_min (oldest pending/processing operation; kept for readers of the
old field, no longer judged on its own), llm_auth_errors (LLM trace rows in the last
hour whose error mentions 401/403), ops_failed (async operations in status `failed`
that failed in the last FAILED_WINDOW_HOURS hours, per operation type: count and the
newest failure's time; {} when none),
mm_paused (mental models whose automatic refresh Hindsight paused after a failure, all
banks, no window: bank, id, name, failed_at, last_refreshed_at; [] when none),
backup_newest (newest pgBackRest backup label and stop time), backup_status,
verify_result (systemd Result of the weekly verify unit), extraction_failures (failure LINES
of the primary extraction entry, gpt-6.1-sol, since `--since`, from the proxy journal),
extraction_failure_first and extraction_failure_last (UTC times of the first and last of
them, null when there were none), extraction_unrecovered (requests gpt-6.1-sol failed that
did not end with status 200, or a failure line with no request id to match its result),
extraction_unrecovered_first and extraction_unrecovered_last (UTC times of their final
lines, or of the failure line when it has no id; null when there were none), and
extraction_recovered (requests gpt-6.1-sol failed that still ended with status 200 after
failover). A request is counted in the window its final line falls in; the journal is read
from LOOKBACK_S before `--since` so a failure just before the window is matched, and a
request whose failure and final line are more than LOOKBACK_S apart can be missed. Which
model answered a request is not reported: nothing on the server records it. The seven are
left out under `--pulse`.

A value that could not be read is null together with an entry in `errors`, never a
guess, so the reader can tell "fine" from "unknown".

The window is since <= time < until for the journal counts and since < ts <= until for
proxy events, all compared as UTC times (review r2), so consecutive laptop runs that pass
the same instant as one run's `--until` and the next run's `--since` count nothing twice
(HOS-417 review r1). A proxy event whose ts does not parse is kept. Without `--until`
the window is open at the end, as before. A missing or unparsable `--since` or `--until`
value exits 2.

    hindsight-status [--since ISO] [--until ISO]  # print the snapshot as JSON
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
problem is work waiting while nothing completes. A pending row the server has deferred
(next_retry_at in the future) is not waiting work (HOS-288, MEASURED 2026-11-01: five
deferred refresh_mental_model rows raised a false stall at 23:01 UTC).

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
AUTH_EXPIRED = Path("/var/lib/cliproxy-update/AUTH_EXPIRED")
EVENTS = Path("/var/lib/cliproxy-update/events.jsonl")
SETTINGS = Path("/etc/default/hindsight-status")  # CERT=<path to the served fullchain.pem>


CERT_MIN_DAYS = 20
DISK_MAX_PCT = 75
# Work waiting and nothing completed for this long is a stall. 30 is HELM's starting
# proposal of 2026-10-01 (the old oldest-item limit carried over), not a measurement.
QUEUE_STALL_MIN = 30
# A failed operation is reported for this long after it failed, then no longer (HOS-288).
FAILED_WINDOW_HOURS = 24
VERIFY_UNIT = "pgbackrest-verify.service"
VERIFY_TIMEOUT_S = 3600
STANZA = "hindsight"
PULSE_URL_FILE = Path("/etc/hindsight/pulse-url")  # Healthchecks.io ping URL, 0600 root
# Hindsight extraction fallback (HOS-400). The proxy serves the alias gpt-6-hs-extract from
# two openai-compatibility entries that point at the proxy itself: hs-extract-1-sol
# (priority 10, model gpt-6.1-sol) and hs-extract-2-luna (priority 0, model gpt-6-luna).
# A request the first entry fails moves to the second in the same request, and the first
# is set aside about 60 s (CLIProxyAPI v8.0.78 source: sdk/cliproxy/auth/
# conductor_execution.go executeMixedOnce, conductor_cooldown.go:1968, read 2026-11-20).
# Contract of the journal line, read 2026-11-20 in the same source
# (conductor_execution.go:2450 and 2453, warnLogUpstreamFailure; internal/util/provider.go:15-27):
#   "<status> | <duration> | upstream execution failed: provider=<key> model=<upstream model> auth=..."
# where <key> is the entry name, possibly prefixed "openai-compatible-". Only FAILURES of
# the primary entry are reported, never which model answered: no proxy log line and no
# llm_requests column names the upstream that served a success (MEASURED 2026-11-20,
# HOS-400 review round 3). Journal records are read as `journalctl -o json`: MESSAGE is a
# string and __REALTIME_TIMESTAMP a decimal string of microseconds since the epoch, UTC
# (MEASURED 2026-11-20). Without --all, journalctl writes a field over 4096 bytes as null
# (journalctl(1), "-o json"), so a failure line carrying a long upstream body would vanish:
# --all is passed, and a record whose MESSAGE is still not text is an error, never skipped.
# ASSUMED 2026-11-20: the provider key form; HELM's live test on hs-test-1-sol shows it.
# Contract of the FINAL line of a request (HOS-417), MEASURED 2026-11-22 by HELM in the
# proxy journal on the memory server, shape (time, id and duration invented here):
#   "[2026-01-01 10:00:10] [0f0f0f0f] [info ] [gin_logger.go:114] 200 |  9.000s |  127.0.0.1 | POST  "/v1/chat/completions""
# A final line is a MESSAGE containing "[gin_logger.go:" and "/v1/chat/completions". The
# request id is the second bracketed token, shared with the failure lines of the same
# request ("[2026-01-01 10:00:00] [0f0f0f0f] [warn ] [conductor_execution.go:3101] 408 | ...
# upstream execution failed: ..." in the same measurement). The status is the first integer
# after the "[gin_logger.go:N]" token, before the first "|"; a status that cannot be parsed
# counts as a non-200 final (fail safe). Several final lines with one id: the last wins.
# A second token holding whitespace ("[warn ]") or no second token means no id.
PROXY_UNIT = "cliproxyapi.service"
EXTRACT_PRIMARY_ENTRY = "hs-extract-1-sol"
EXTRACT_PRIMARY = "gpt-6.1-sol"
LOOKBACK_S = 900
_UPSTREAM_FAILED = re.compile(r"upstream execution failed: provider=(\S+) model=(\S+)")
_BRACKETS = re.compile(r"\[([^\]]*)\]")
_FINAL_STATUS = re.compile(r"\[gin_logger\.go:[^\]]*\]([^|]*)\|")
# The second copy of this table is in scripts/server/cliproxy-update.py (LOGIN_COMMANDS);
# the two files are installed separately, so each carries its own.
LOGIN_COMMANDS = {"claude": "cli-proxy-api -claude-login -no-browser as user cliproxyapi, "
                            "callback tunnel on port 54545",
                  "codex": "cli-proxy-api -codex-device-login as user cliproxyapi"}


def login_hint(provider: str) -> str:
    names = provider.split(",")
    known = [LOGIN_COMMANDS[n] for n in names if n in LOGIN_COMMANDS]
    if len(known) < len(names):
        known.append("see the proxy login guide")
    return " and ".join(known)


def _journal_records(raw: str) -> list[tuple[datetime, str]]:
    """(time, message) per line of `journalctl -o json` output (pure). A MESSAGE journald
    stored as binary arrives as a list of byte values and is decoded as UTF-8. A line that
    is not a journal record, and a MESSAGE that is null, absent or not decodable, raise
    ValueError: an unreadable record could be a failure, so the window's count is unknown."""
    out = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        msg = rec.get("MESSAGE")
        if isinstance(msg, list):
            try:
                msg = bytes(msg).decode("utf-8")
            except (TypeError, ValueError) as exc:
                raise ValueError(f"unreadable journal record: MESSAGE is not decodable ({exc})") from exc
        if not isinstance(msg, str):
            raise ValueError(f"unreadable journal record: MESSAGE is {type(msg).__name__}")
        stamp = datetime.fromtimestamp(int(rec["__REALTIME_TIMESTAMP"]) / 1_000_000, timezone.utc)
        out.append((stamp, msg))
    return out


def _is_primary_failure(line: str) -> bool:
    """Whether a journal MESSAGE is a failure line of the primary extraction entry (pure).
    A failure line whose shape no longer matches the contract above, or one naming the
    primary entry with another model, raises ValueError: the proxy or its config changed,
    and a count taken anyway could be wrong."""
    if "upstream execution failed" not in line:
        return False
    m = _UPSTREAM_FAILED.search(line)
    if m is None:
        # The credential part of a line (auth=...) never goes into the message.
        raise ValueError(f"unrecognised proxy failure line: {line.split(' auth=', 1)[0][:120]}")
    provider, model = m.groups()
    if provider.removeprefix("openai-compatible-") != EXTRACT_PRIMARY_ENTRY:
        return False
    if model != EXTRACT_PRIMARY:
        raise ValueError(f"{EXTRACT_PRIMARY_ENTRY} failed with model {model}, expected {EXTRACT_PRIMARY}")
    return True


def _request_id(line: str) -> str | None:
    """The proxy's request id, the second bracketed token, or None (contract above)."""
    tokens = _BRACKETS.findall(line)
    if len(tokens) < 2 or not tokens[1] or any(c.isspace() for c in tokens[1]):
        return None
    return tokens[1]


def _final_status(line: str) -> int | None:
    """The status of a final line, or None when it cannot be parsed (contract above)."""
    m = _FINAL_STATUS.search(line)
    digits = re.search(r"\d+", m.group(1)) if m else None
    return int(digits.group()) if digits else None


def _in_window(stamp: datetime, since: datetime | None, until: datetime | None) -> bool:
    """since <= stamp < until, either bound absent meaning open (HOS-417 review r1)."""
    return (since is None or stamp >= since) and (until is None or stamp < until)


def _utc_time(stamp: str) -> datetime:
    """An ISO 8601 time as an aware UTC datetime; a time with no offset is read as local,
    as journal_since reads it. Raises ValueError (or TypeError) when it does not parse."""
    return datetime.fromisoformat(stamp).astimezone(timezone.utc)


def event_in_window(ts, since: datetime, until: datetime | None) -> bool:
    """since < ts <= until for a proxy event, compared as UTC times, never as strings
    (HOS-417 review r2: `2026-01-01T11:00:00+00:00` is after a `--until` of
    `2026-01-01T12:00:00+02:00`, though it sorts before it as text). An event whose ts is
    missing or does not parse is KEPT, so it is reported rather than dropped silently."""
    try:
        t = _utc_time(ts)
    except (TypeError, ValueError, OverflowError):
        return True
    return t > since and (until is None or t <= until)


def extraction_failures(raw: str, since: datetime | None = None, until: datetime | None = None) -> dict:
    """Failure LINES of the primary extraction entry in `journalctl -o json` output (pure):
    {"count": n, "first": ISO or None, "last": ISO or None}, times in UTC, only lines with
    since <= time < until for the bounds given. Raises ValueError as _is_primary_failure does."""
    times = [stamp for stamp, line in _journal_records(raw)
             if _is_primary_failure(line) and _in_window(stamp, since, until)]
    iso = [t.isoformat(timespec="seconds") for t in sorted(times)]
    return {"count": len(iso), "first": iso[0] if iso else None, "last": iso[-1] if iso else None}


def extraction_outcomes(raw: str, since: datetime, until: datetime | None = None) -> dict:
    """What became of the requests the primary extraction entry failed (pure):
    {"unrecovered": n, "unrecovered_first": ISO or None, "unrecovered_last": ISO or None,
    "recovered": n}, times in UTC. `raw` may start before `since` (main reads LOOKBACK_S
    earlier) so that a failure just before the window is matched with its final line.
    A request with an id is one request however many failure lines it has, and is counted
    in the window its final line falls in (time >= since): RECOVERED when that line's
    status is 200, UNRECOVERED otherwise, an unparsable status included. A failure with no
    final line yet is counted nowhere and is seen next window through the lookback, so a
    request whose failure and final line lie more than LOOKBACK_S apart can be missed. A
    failure line with no id is UNRECOVERED, counted where it falls (time >= since). With
    `until`, a line at or after it belongs to the next window: a final line there is not
    seen yet, and a failure line without an id there is not counted.
    Raises ValueError as _is_primary_failure does."""
    failed_ids: set[str] = set()
    finals: dict[str, tuple[datetime, int | None]] = {}
    unrecovered: list[datetime] = []
    for stamp, line in _journal_records(raw):
        if until is not None and stamp >= until:
            continue
        if _is_primary_failure(line):
            rid = _request_id(line)
            if rid is None:
                if stamp >= since:
                    unrecovered.append(stamp)
            else:
                failed_ids.add(rid)
        elif "[gin_logger.go:" in line and "/v1/chat/completions" in line:
            rid = _request_id(line)
            if rid is not None:
                finals[rid] = (stamp, _final_status(line))
    recovered = 0
    for rid in failed_ids:
        if rid not in finals:
            continue
        stamp, status = finals[rid]
        if stamp < since:
            continue
        if status == 200:
            recovered += 1
        else:
            unrecovered.append(stamp)
    iso = [t.isoformat(timespec="seconds") for t in sorted(unrecovered)]
    return {"unrecovered": len(iso), "unrecovered_first": iso[0] if iso else None,
            "unrecovered_last": iso[-1] if iso else None, "recovered": recovered}


def journal_since(since: str) -> str:
    """`--since` as journalctl reads it, from the ISO 8601 time the laptop passes."""
    return datetime.fromisoformat(since).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


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
    if snap.get("auth_expired"):
        provider = snap["auth_expired"].split()[0]
        out.append(f"CLIProxyAPI on the memory server: the {provider} login expired; "
                   f"log in again ({login_hint(provider)})")
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
    paused = snap.get("mm_paused")
    if paused:
        names = "; ".join(m["name"] if m.get("bank") == "main" else f"{m['name']} [{m.get('bank')}]"
                          for m in paused)
        out.append(f"Hindsight mental model refresh paused after a failure: {len(paused)} ({names}); "
                   "automatic refresh stays off until an explicit refresh")
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
    Only failures of the last FAILED_WINDOW_HOURS hours are counted (HOS-288), so an old
    failure stops repeating in every alert. A failure that a later completed operation of
    the same type on the same target has superseded is not counted (HOS-289, MEASURED
    2026-11-01: a mental model page failed at 18:26 UTC and refreshed at 19:05 UTC, and
    the alert repeated the failure all day). A snapshot without the field comes from a
    status script older than HOS-253 and is not judged on it."""
    failed = snap.get("ops_failed") or {}
    total = sum(v["count"] for v in failed.values())
    if not total:
        return None
    parts = [f"{t} {v['count']}" + (f", newest {v['newest']}" if v.get("newest") else "")
             for t, v in sorted(failed.items())]
    return f"Hindsight operations failed: {total} in the last {FAILED_WINDOW_HOURS} h ({'; '.join(parts)})"


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

    def extraction_raises(text: str) -> bool:
        try:
            extraction_failures(text)
        except ValueError:
            return True
        return False

    def jr(second: int, message) -> str:
        """One invented `journalctl -o json` record, `second` seconds after 2026-01-01 10:00 UTC."""
        return json.dumps({"MESSAGE": message, "__REALTIME_TIMESTAMP": str((1767261600 + second) * 1_000_000)})

    # Invented journal text in the shape of conductor_execution.go:2450/2453.
    ok_line = '[aaaa0001] [info ] [gin_logger.go:114] 200 |  7.8s | 127.0.0.1 | POST "/v1/chat/completions"'
    codex_fail = ("[aaaa0002] [warn ] [conductor_execution.go:2450] 502 |  916ms | "
                  "upstream execution failed: provider=codex model=gpt-6.1-sol auth=x err=overloaded")
    sol_fail = ("[aaaa0003] [warn ] [conductor_execution.go:2450] 503 |  3ms | "
                "upstream execution failed: provider=hs-extract-1-sol model=gpt-6.1-sol auth=x err=auth_unavailable")
    sol_fail_prefixed = sol_fail.replace("provider=hs-extract-1-sol", "provider=openai-compatible-hs-extract-1-sol")
    sol_fail_no_status = ("[aaaa0004] [warn ] [conductor_execution.go:2453] "
                          "upstream execution failed: provider=hs-extract-1-sol model=gpt-6.1-sol auth=x duration=2m0s err=timeout")
    luna_fail = sol_fail.replace("hs-extract-1-sol model=gpt-6.1-sol", "hs-extract-2-luna model=gpt-6-luna")
    both = "\n".join([jr(0, ok_line), jr(61, codex_fail), jr(62, sol_fail), jr(70, ok_line),
                      jr(125, sol_fail_prefixed), jr(180, sol_fail_no_status)])
    no_primary = "\n".join([jr(0, ok_line), jr(1, codex_fail), jr(2, luna_fail), jr(3, ok_line)])
    as_bytes = jr(5, list(sol_fail.encode()))
    # Invented lines in the shape HELM measured 2026-11-22 (invented ids, no real key).
    def fail_id(rid: str) -> str:
        return (f"[2026-01-01 10:00:00] [{rid}] [warn ] [conductor_execution.go:3101] 408 |  4.5s | "
                "upstream execution failed: provider=openai-compatible-hs-extract-1-sol model=gpt-6.1-sol "
                "auth=api_key=xxxx...xxxx err=stream closed")

    def final(rid: str, status: str = "200") -> str:
        return (f'[2026-01-01 10:00:10] [{rid}] [info ] [gin_logger.go:114] {status} |  14.0s |  '
                '127.0.0.1 | POST  "/v1/chat/completions"')

    win = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)  # second 0 of jr

    def outcome(*records: str, since: datetime = win) -> dict:
        return extraction_outcomes("\n".join(records), since)

    none_out = {"unrecovered": 0, "unrecovered_first": None, "unrecovered_last": None, "recovered": 0}
    lookback = [jr(-30, fail_id("bbbb0001")), jr(20, final("bbbb0001", "502"))]
    bounded = [jr(1, fail_id("bbbb000a")), jr(11, final("bbbb000a", "500"))]

    def args_exit(args: list[str]) -> int:
        """main's exit code on an argument error; it returns before any system call."""
        with contextlib.redirect_stderr(io.StringIO()):
            return main(["hindsight-status", *args])

    cases = [
        ("healthy snapshot raises nothing", n() == 0),
        ("long queue that is moving raises nothing",
         n(queue_waiting=106, queue_oldest_min=300.0, queue_last_done_min=0.0) == 0),
        ("waiting work with nothing completed for 45 min raised",
         n(queue_waiting=5, queue_oldest_min=50.0, queue_last_done_min=45.0) == 1),
        ("empty queue raises nothing however long since the last completion",
         n(queue_waiting=0, queue_oldest_min=0.0, queue_last_done_min=900.0) == 0),
        ("waiting work with nothing completed for 45 min raised, oldest unknown",
         n(queue_waiting=5, queue_last_done_min=45.0) == 1),
        ("failed operations reported with their window",
         problems({**good, "ops_failed": {"refresh_mental_model": {"count": 2, "newest": "2026-09-30T20:00:00+00:00"}}},
                  now)
         == ["Hindsight operations failed: 2 in the last 24 h "
             "(refresh_mental_model 2, newest 2026-09-30T20:00:00+00:00)"]),
        ("no failed operations in the window raises nothing", n(ops_failed={}) == 0),
        ("paused mental models raised by name, in list order",
         problems({**good, "mm_paused": [
             {"bank": "main", "id": "a1", "name": "Alpha notes", "failed_at": "2026-09-30T20:00:00+00:00",
              "last_refreshed_at": "2026-09-30T10:00:00+00:00"},
             {"bank": "main", "id": "b2", "name": "Beta notes", "failed_at": "2026-09-30T21:00:00+00:00",
              "last_refreshed_at": None}]}, now)
         == ["Hindsight mental model refresh paused after a failure: 2 (Alpha notes; Beta notes); "
             "automatic refresh stays off until an explicit refresh"]),
        ("paused mental model in another bank shows the bank",
         problems({**good, "mm_paused": [
             {"bank": "otherbank", "id": "c3", "name": "Gamma notes", "failed_at": "2026-09-30T20:00:00+00:00",
              "last_refreshed_at": None}]}, now)
         == ["Hindsight mental model refresh paused after a failure: 1 (Gamma notes [otherbank]); "
             "automatic refresh stays off until an explicit refresh"]),
        ("no paused mental model, or no field, raises nothing",
         n(mm_paused=[]) == 0 and n(mm_paused=None) == 0 and "mm_paused" not in good and n() == 0),
        ("waiting work and nothing ever completed, oldest 45 min, raised",
         n(queue_waiting=2, queue_oldest_min=45.0, queue_last_done_min=None) == 1),
        ("waiting work and nothing ever completed, oldest 10 min, raises nothing",
         n(queue_waiting=2, queue_oldest_min=10.0, queue_last_done_min=None) == 0),
        ("snapshot from the old status script still judged by oldest item",
         len(problems({k: v for k, v in good.items() if k not in ("queue_waiting", "queue_last_done_min")}
                      | {"queue_oldest_min": 45.0}, now)) == 1),
        ("unreadable queue reported once, through errors",
         n(queue_waiting=None, queue_oldest_min=None, queue_last_done_min=None, errors=["queue: x"]) == 1),
        ("expired proxy login raised with the login command",
         problems({**good, "auth_expired": "claude 2026-10-30T02:00:00+00:00 Claude ZEBRA test: x"}, now)
         == ["CLIProxyAPI on the memory server: the claude login expired; log in again "
             "(cli-proxy-api -claude-login -no-browser as user cliproxyapi, callback tunnel on port 54545)"]),
        ("no expired proxy login raises nothing", n(auth_expired=None) == 0),
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
        ("every primary failure form counted with its first and last time, the codex inner hop not",
         extraction_failures(both) == {"count": 3, "first": "2026-01-01T10:01:02+00:00",
                                       "last": "2026-01-01T10:03:00+00:00"}),
        ("a backup entry or codex failure alone counts nothing",
         extraction_failures(no_primary) == {"count": 0, "first": None, "last": None}),
        ("empty journal counts nothing", extraction_failures("") == {"count": 0, "first": None, "last": None}),
        ("a MESSAGE stored as bytes is still read", extraction_failures(as_bytes)["count"] == 1),
        ("a line that is not a journal record refused", extraction_raises("not json")),
        ("a null MESSAGE (a long field without --all) is an error, not zero",
         extraction_raises(jr(0, None))),
        ("an absent MESSAGE is an error", extraction_raises(json.dumps({"__REALTIME_TIMESTAMP": "0"}))),
        ("an undecodable MESSAGE is an error", extraction_raises(jr(0, [0xff, 0xfe]))),
        ("a long failure line delivered whole is counted", extraction_failures(jr(0, sol_fail + "x" * 10000))["count"] == 1),
        ("unparseable failure line refused",
         extraction_raises(jr(0, ok_line) + "\n" + jr(1, "upstream execution failed: something else"))),
        ("primary entry on another model refused",
         extraction_raises(jr(0, sol_fail.replace("model=gpt-6.1-sol", "model=gpt-6-luna")))),
        ("--since converted for journalctl",
         journal_since("2026-11-20T19:00:00+04:00") == "2026-11-20 15:00:00 UTC"),
        ("a failover that ended 200 is recovered, not unrecovered",
         outcome(jr(1, fail_id("bbbb0002")), jr(11, final("bbbb0002"))) == {**none_out, "recovered": 1}),
        ("a non-200 final is unrecovered at the final line's time",
         outcome(jr(1, fail_id("bbbb0003")), jr(11, final("bbbb0003", "500")))
         == {**none_out, "unrecovered": 1, "unrecovered_first": "2026-01-01T10:00:11+00:00",
             "unrecovered_last": "2026-01-01T10:00:11+00:00"}),
        ("an unparsable final status is unrecovered",
         outcome(jr(1, fail_id("bbbb0004")), jr(11, final("bbbb0004", "---")))["unrecovered"] == 1),
        ("a failure in the lookback with its final line in the window is counted once",
         outcome(*lookback)["unrecovered"] == 1),
        ("the lookback does not change extraction_failures",
         extraction_failures("\n".join(lookback), win)["count"] == 0),
        ("the previous window, which holds no final line, counts nothing for that request",
         outcome(lookback[0], since=win - timedelta(seconds=60)) == none_out),
        ("a final line before the window is not counted again",
         outcome(jr(-30, fail_id("bbbb0005")), jr(-20, final("bbbb0005", "500"))) == none_out),
        ("a failure with no final line counts nothing", outcome(jr(1, fail_id("bbbb0006"))) == none_out),
        ("duplicate failure lines with one id are one request",
         outcome(jr(1, fail_id("bbbb0007")), jr(2, fail_id("bbbb0007")), jr(11, final("bbbb0007", "500")))["unrecovered"] == 1),
        ("several final lines with one id: the last wins",
         outcome(jr(1, fail_id("bbbb0008")), jr(5, final("bbbb0008", "500")), jr(11, final("bbbb0008")))
         == {**none_out, "recovered": 1}),
        ("a failure line with no id is unrecovered at its own time",
         outcome(jr(62, sol_fail)) == {**none_out, "unrecovered": 1, "unrecovered_first": "2026-01-01T10:01:02+00:00",
                                       "unrecovered_last": "2026-01-01T10:01:02+00:00"}),
        ("a success with no primary failure counts nothing", outcome(jr(1, final("bbbb0009"))) == none_out),
        ("a final line at until is not counted in this window",
         extraction_outcomes("\n".join(bounded), win, win + timedelta(seconds=11)) == none_out),
        ("the same final line is counted in the next window",
         extraction_outcomes("\n".join(bounded), win + timedelta(seconds=11), win + timedelta(seconds=60))
         == {**none_out, "unrecovered": 1, "unrecovered_first": "2026-01-01T10:00:11+00:00",
             "unrecovered_last": "2026-01-01T10:00:11+00:00"}),
        ("a failure line at until is left to the next window",
         extraction_failures(jr(11, sol_fail), win, win + timedelta(seconds=11))["count"] == 0
         and extraction_failures(jr(11, sol_fail), win + timedelta(seconds=11), None)["count"] == 1),
        ("a bare --until exits 2", args_exit(["--until"]) == 2),
        ("a malformed --until exits 2", args_exit(["--until", "yesterday"]) == 2),
        ("--until followed by another flag exits 2", args_exit(["--until", "--pulse"]) == 2),
        ("a malformed --since exits 2", args_exit(["--since", "not-a-time"]) == 2),
        ("an event after an offset --until is excluded (11:00Z, until 12:00+02:00)",
         not event_in_window("2026-01-01T11:00:00+00:00", _utc_time("2026-01-01T09:00:00+00:00"),
                             _utc_time("2026-01-01T12:00:00+02:00"))),
        ("an event before an offset --until is kept (09:30Z, until 12:00+02:00)",
         event_in_window("2026-01-01T09:30:00+00:00", _utc_time("2026-01-01T09:00:00+00:00"),
                         _utc_time("2026-01-01T12:00:00+02:00"))),
        ("an event before an offset --since is excluded (09:30Z, since 12:00+02:00)",
         not event_in_window("2026-01-01T09:30:00+00:00", _utc_time("2026-01-01T12:00:00+02:00"), None)),
        ("an event after an offset --since is kept (10:30Z, since 12:00+02:00)",
         event_in_window("2026-01-01T10:30:00+00:00", _utc_time("2026-01-01T12:00:00+02:00"), None)),
        ("an event at since is excluded and at until is kept",
         not event_in_window("2026-01-01T10:00:00+00:00", win, None)
         and event_in_window("2026-01-01T10:00:00+00:00", win - timedelta(hours=1), win)),
        ("an event whose ts does not parse is kept", event_in_window("garbage", win, win + timedelta(hours=1))),
        ("an event with no ts is kept", event_in_window(None, win, None)),
        ("an event at the edge of the date range is kept, not a crash",
         event_in_window("0001-01-01T00:00:00+01:00", win, None)),
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
    since, until = "1970-01-01T00:00:00+00:00", None
    for flag in ("--since", "--until"):
        if flag not in argv:
            continue
        rest = argv[argv.index(flag) + 1:]
        try:
            if not rest or rest[0].startswith("--"):
                raise ValueError("no value")
            _utc_time(rest[0])
        except (ValueError, OverflowError):
            print(f"hindsight-status: {flag} needs an ISO 8601 time", file=sys.stderr)
            return 2
        if flag == "--since":
            since = rest[0]
        else:
            until = rest[0]
    since_dt = _utc_time(since)
    until_dt = _utc_time(until) if until else None
    out: dict = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "errors": []}

    out["services"] = {}
    for u in UNITS:
        r = subprocess.run(["systemctl", "is-active", u], capture_output=True, text=True, check=False)
        out["services"][u] = r.stdout.strip() or "unknown"

    out["halt"] = HALT.read_text(encoding="utf-8").strip() if HALT.exists() else None
    try:
        out["auth_expired"] = AUTH_EXPIRED.read_text(encoding="utf-8").strip() if AUTH_EXPIRED.exists() else None
    except OSError as exc:
        out["auth_expired"] = None
        out["errors"].append(f"auth_expired: {exc}")
    out["proxy_events"] = []
    if EVENTS.exists():
        for line in EVENTS.read_text(encoding="utf-8").splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                out["errors"].append("unparseable line in events.jsonl")
                continue
            if event_in_window(ev.get("ts"), since_dt, until_dt) and ev.get("event") in ("updated", "update_failed", "rolled_back",
                                                                  "halt", "blacklisted", "download_refused", "error",
                                                                  "auth_expired", "update_deferred",
                                                                  "preflight_failed"):
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
        # A pending row counts as waiting only when it is due. Contract as observed
        # 2026-11-01 through the MCP list_operations/get_operation of hindsight-main: the
        # operation listing exposes next_retry_at, and the server defers
        # refresh_mental_model rows by setting it in the future (each equal to its page's
        # last_refreshed_at plus 6 hours); the deferral is the server's own schedule, not
        # a stall. processing rows count as before.
        # ASSUMED 2026-11-01: the async_operations column is next_retry_at, as the API field is named; HELM confirms on the first snapshot after deploy
        v = psql("select count(*) filter (where status = 'processing' or (status = 'pending' "
                 "and (next_retry_at is null or next_retry_at <= now()))), "
                 "coalesce(extract(epoch from now() - min(created_at) filter "
                 "(where status = 'processing' or (status = 'pending' "
                 "and (next_retry_at is null or next_retry_at <= now()))))/60, 0), "
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
        # row back to 'pending', so a retried failure leaves this count. Only failures of
        # the last FAILED_WINDOW_HOURS hours are counted (HOS-288). Read-only. The only
        # value built into the SQL is int(FAILED_WINDOW_HOURS), a constant: no input.
        # A failure a later completed operation of the same type on the same target has
        # superseded is not counted (HOS-289). Contract read 2026-11-01 in the same
        # Hindsight 1.1.0 source: the target is not a column. async_operations.result_metadata
        # is JSONB NOT NULL default '{}' (alembic/versions/5a366d414dce_initial_schema.py,
        # line 230); refresh_mental_model is submitted with result_metadata
        # {"mental_model_id": ..., "name": ...} (engine/memory_engine.py, line 24196), and
        # list_operations reads the API fields mental_model_id and document_id as
        # result_metadata->'mental_model_id' and ->'document_id' (engine/memory_engine.py,
        # lines 22018 and 22022). The target is the mental model when the row names one,
        # else the document; a row naming neither keeps counting. bank_id is matched too,
        # because a document id is only unique inside its bank.
        v = psql("select f.operation_type, count(*), to_char(max(coalesce(f.completed_at, f.updated_at)) "  # noqa: S608  # nosec B608 - constants only
                 "at time zone 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"+00:00\"') "
                 "from async_operations f where f.status = 'failed' and coalesce(f.completed_at, f.updated_at) "
                 f"> now() - interval '{int(FAILED_WINDOW_HOURS)} hours' "
                 "and not exists (select 1 from async_operations c where c.status = 'completed' "
                 "and c.operation_type = f.operation_type and c.bank_id = f.bank_id "
                 "and coalesce(c.completed_at, c.updated_at) > coalesce(f.completed_at, f.updated_at) "
                 "and ((f.result_metadata->>'mental_model_id' is not null "
                 "and c.result_metadata->>'mental_model_id' = f.result_metadata->>'mental_model_id') "
                 "or (f.result_metadata->>'mental_model_id' is null "
                 "and f.result_metadata->>'document_id' is not null "
                 "and c.result_metadata->>'document_id' = f.result_metadata->>'document_id'))) "
                 "group by f.operation_type")
        failed = {}
        for line in v.splitlines():
            op_type, count, newest = line.rsplit("|", 2)
            failed[op_type] = {"count": int(count), "newest": newest or None}
        out["ops_failed"] = failed
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        out["ops_failed"] = None
        out["errors"].append(f"failed operations: {exc}")
    try:
        # Hindsight v1.2.3 pauses automatic refresh (cron and post-consolidation) of a
        # mental model whose last refresh failed after its last success, until an explicit
        # refresh (upstream #4532; _MM_REFRESH_PAUSED_SQL, hindsight_api/engine/
        # memory_engine.py lines 291-293, read 2026-11-20). The same predicate here, all
        # banks, no window, so a paused model is reported for as long as it stays paused
        # (HOS-399). Read-only; the SQL holds no input. name is last: it is free text.
        v = psql("select bank_id, "
                 "to_char(last_refresh_failed_at at time zone 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"+00:00\"'), "
                 "coalesce(to_char(last_refreshed_at at time zone 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"+00:00\"'), ''), "
                 "id, name from mental_models where last_refresh_failed_at is not null "
                 "and (last_refreshed_at is null or last_refresh_failed_at > last_refreshed_at) "
                 "order by bank_id, name")
        paused = []
        for line in v.splitlines():
            bank, failed_at, refreshed_at, mm_id, name = line.split("|", 4)
            paused.append({"bank": bank, "id": mm_id, "name": name, "failed_at": failed_at,
                           "last_refreshed_at": refreshed_at or None})
        out["mm_paused"] = paused
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        out["mm_paused"] = None
        out["errors"].append(f"mental models: {exc}")
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

    out["extraction_failures"] = out["extraction_failure_first"] = out["extraction_failure_last"] = None
    out["extraction_unrecovered"] = out["extraction_unrecovered_first"] = None
    out["extraction_unrecovered_last"] = out["extraction_recovered"] = None
    try:
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        read_from = max(since_dt - timedelta(seconds=LOOKBACK_S), epoch)
        bound = ["--until", journal_since(until)] if until else []
        raw = run(["journalctl", "-u", PROXY_UNIT, "--since", journal_since(read_from.isoformat()), *bound,
                   "-o", "json", "--all", "--output-fields=MESSAGE", "--no-pager"])
        found = extraction_failures(raw, since_dt, until_dt)
        outcomes = extraction_outcomes(raw, since_dt, until_dt)
        out["extraction_failures"] = found["count"]
        out["extraction_failure_first"], out["extraction_failure_last"] = found["first"], found["last"]
        out["extraction_unrecovered"] = outcomes["unrecovered"]
        out["extraction_unrecovered_first"] = outcomes["unrecovered_first"]
        out["extraction_unrecovered_last"] = outcomes["unrecovered_last"]
        out["extraction_recovered"] = outcomes["recovered"]
    except (subprocess.SubprocessError, OSError, ValueError, OverflowError, KeyError) as exc:
        out["errors"].append(f"extraction failures: {exc}")
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
