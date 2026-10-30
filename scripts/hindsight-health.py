#!/usr/bin/env python3
"""Hourly health check of a remote Hindsight deployment, run from the operator's laptop.

    hindsight-health.py check       # evaluate, notify on a change of state, exit 1 if unhealthy
    hindsight-health.py show        # evaluate and print, never notify
    hindsight-health.py self-test   # the problem rules on fixed snapshots, no network

The server holds no credential for any notification channel. This laptop asks it
for a read-only snapshot through a dedicated ssh user whose forced command prints
JSON (`scripts/server/hindsight-status.py`), checks what only the laptop can see
(the tunnel, the web UI through it, the age of the newest verified backup copy
here), and tells the operator through his own sink. So a message can reach no one
but him.

Problems raised: tunnel down or API not ready; web UI not answering over TLS; newest
verified local backup older than `HINDSIGHT_HEALTH_BACKUP_MAX_H` (default 26); server
unreachable; any watched service not active; cliproxy-update HALT; an expired
subscription login of the proxy (cliproxy-update AUTH_EXPIRED, with the login command,
for as long as the file exists); any proxy update, rollback, refusal, deferral or
expired-login event since the previous run; certificate on the server with fewer
than 20 days left (the Porkbun renewal did not arrive); root disk above 75 %;
Hindsight operations waiting while none has completed for 30 minutes; LLM 401/403
in the last hour (the first sign of an account problem); any Hindsight async
operation in status failed, as one line with the total and, per operation type, the
count and the newest failure's time (read only: nothing here retries, cancels or
deletes an operation); server backup status not ok; the server's weekly backup verify failed; any field the server could not read.

A message goes out only when the set of problems CHANGES, including the all-clear,
so an offline laptop or a known fault does not repeat every hour. Proxy events are
reported once each. Settings from the gitignored `.env`: `HINDSIGHT_HEALTH_HOST`
(ssh alias of the monitor user), `HINDSIGHT_HEALTH_UI_NAME` (the FQDN served
through the tunnel), `HINDSIGHT_HEALTH_API_URL` (default http://127.0.0.1:8888),
`HINDSIGHT_BACKUP_DIR` (shared with backup-pull.py).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.api import load_api_key  # noqa: E402
from scripts.utils.telegram_notify import notify, own_targets  # noqa: E402

STATE = Path.home() / ".local" / "state" / "hindsight-health.json"
SSH_TIMEOUT_S = 60


def _setting(name: str, default: str | None = None) -> str:
    value = load_api_key(name, required=False) or default
    if not value:
        raise ValueError(f"{name} is not set in .env")
    return value


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load_status_module():
    """The server's own status script holds the judging rules; load it by path (its
    file name has a dash) so the laptop and the server's pulse can never disagree."""
    path = Path(__file__).resolve().parent / "server" / "hindsight-status.py"
    spec = importlib.util.spec_from_file_location("hindsight_status", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


server_problems = _load_status_module().problems


def _snapshot(host: str, since: str) -> dict:
    proc = subprocess.run(["ssh", host, "--", "--since", since], capture_output=True, text=True,
                          timeout=SSH_TIMEOUT_S, check=False)
    if proc.returncode != 0:
        raise OSError(f"ssh {host} exit {proc.returncode}: {proc.stderr.strip()[-300:]}")
    return json.loads(proc.stdout)


def _ui_ok(name: str) -> str | None:
    """None when the UI answers over TLS through the tunnel on 127.0.0.1:443."""
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection(("127.0.0.1", 443), timeout=10) as raw, \
                ctx.wrap_socket(raw, server_hostname=name) as tls:
            tls.sendall(f"HEAD / HTTP/1.1\r\nHost: {name}\r\nConnection: close\r\n\r\n".encode())
            status = tls.recv(64).decode("latin-1").split(" ")
        code = int(status[1]) if len(status) > 1 and status[1].isdigit() else 0
        return None if code in (200, 307, 308) else f"web UI answered {code}"
    except (OSError, ssl.SSLError) as exc:
        return f"web UI not reachable over TLS: {exc}"


def evaluate(now: datetime, state: dict) -> tuple[list[str], list[dict]]:
    problems: list[str] = []
    api = _setting("HINDSIGHT_HEALTH_API_URL", "http://127.0.0.1:8888")
    if not api.startswith(("http://", "https://")):
        raise ValueError(f"HINDSIGHT_HEALTH_API_URL must be http(s): {api}")
    try:
        with urllib.request.urlopen(f"{api}/health/ready", timeout=10) as r:  # noqa: S310 - scheme checked above
            if r.status != 200:
                problems.append(f"Hindsight API not ready ({r.status})")
    except (urllib.error.URLError, OSError) as exc:
        problems.append(f"tunnel down or API not ready: {exc}")
    ui = _ui_ok(_setting("HINDSIGHT_HEALTH_UI_NAME"))
    if ui:
        problems.append(ui)
    root = Path(_setting("HINDSIGHT_BACKUP_DIR")).expanduser()
    max_h = float(_setting("HINDSIGHT_HEALTH_BACKUP_MAX_H", "26"))
    last = root / "LAST_VERIFIED"
    if not last.exists():
        problems.append("no verified backup copy on this laptop")
    else:
        taken = datetime.strptime(last.read_text(encoding="utf-8").strip(), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        age_h = (now - taken).total_seconds() / 3600
        if age_h > max_h:
            problems.append(f"newest verified backup copy here is {age_h:.0f} h old")
    events: list[dict] = []
    try:
        snap = _snapshot(_setting("HINDSIGHT_HEALTH_HOST"), state.get("last_run", "1970-01-01T00:00:00+00:00"))
        problems += server_problems(snap, now)
        events = snap.get("proxy_events", [])
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        problems.append(f"server unreachable for the health check: {exc}")
    return problems, events


def _send(text: str) -> None:
    delivered = sum(1 for t in sorted(own_targets()) if notify(t, text))
    if not delivered:
        print("hindsight-health: NO NOTIFICATION SENT, no operator sink is configured or the"
              " transport refused.", file=sys.stderr)


def cmd_check(args) -> int:
    now = _now()
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    problems, events = evaluate(now, state)
    for p in problems:
        print(f"PROBLEM {p}")
    for ev in events:
        print(f"EVENT {json.dumps(ev, ensure_ascii=False)}")
    if not args.dry:
        before = set(state.get("problems", []))
        lines = []
        if set(problems) != before:
            lines += [f"- {p}" for p in problems] if problems else ["all clear again"]
        lines += [f"- proxy {ev.get('event')}: {ev.get('tag', '')} {ev.get('reason', '')}".rstrip() for ev in events]
        if lines:
            _send("HEADING OS memory server:\n" + "\n".join(lines))
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_name(STATE.name + ".tmp")
        tmp.write_text(json.dumps({"problems": problems, "last_run": now.isoformat(timespec="seconds")}), encoding="utf-8")
        os.replace(tmp, STATE)
    if not problems:
        print("healthy")
    return 1 if problems else 0


def cmd_self_test(_args) -> int:
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    good = {"services": {"a": "active"}, "halt": None, "cert_not_after": "2026-12-28T14:06:55+00:00",
            "disk_pct": 4.0, "queue_waiting": 0, "queue_oldest_min": 0.0, "queue_last_done_min": 3.0,
            "llm_auth_errors": 0, "backup_status": "ok", "verify_result": "success", "errors": []}
    cases = [
        ("healthy snapshot raises nothing", server_problems(good, now) == []),
        ("inactive service raised", len(server_problems({**good, "services": {"a": "failed"}}, now)) == 1),
        ("HALT raised", len(server_problems({**good, "halt": "rollback failed"}, now)) == 1),
        ("expired proxy login raised", server_problems({**good, "auth_expired": "claude 2026-10-30T02:00:00+00:00 x"}, now)
         == ["CLIProxyAPI on the memory server: the claude login expired; log in again "
             "(cli-proxy-api -claude-login -no-browser as user cliproxyapi, callback tunnel on port 54545)"]),
        ("no expired proxy login raises nothing", server_problems({**good, "auth_expired": None}, now) == []),
        ("cert under 20 days raised", len(server_problems({**good, "cert_not_after": "2026-10-15T00:00:00+00:00"}, now)) == 1),
        ("disk over 75 % raised", len(server_problems({**good, "disk_pct": 80.0}, now)) == 1),
        ("stalled queue raised", len(server_problems({**good, "queue_waiting": 5, "queue_oldest_min": 50.0,
                                                      "queue_last_done_min": 45.0}, now)) == 1),
        ("long moving queue raises nothing", server_problems({**good, "queue_waiting": 106, "queue_oldest_min": 300.0,
                                                              "queue_last_done_min": 0.0}, now) == []),
        ("failed weekly verify raised", len(server_problems({**good, "verify_result": "exit-code"}, now)) == 1),
        ("LLM 401/403 raised", len(server_problems({**good, "llm_auth_errors": 2}, now)) == 1),
        ("unreadable field raised", len(server_problems({**good, "errors": ["cert: x"]}, now)) == 1),
        ("backup status not ok raised", len(server_problems({**good, "backup_status": "error"}, now)) == 1),
    ]
    failures = 0
    for label, ok in cases:
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {label}")
    print(f"{len(cases) - failures}/{len(cases)} passed")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check").set_defaults(fn=cmd_check, dry=False)
    sub.add_parser("show").set_defaults(fn=cmd_check, dry=True)
    sub.add_parser("self-test").set_defaults(fn=cmd_self_test)
    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except ValueError as exc:
        print(f"hindsight-health: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
