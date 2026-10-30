#!/usr/bin/env python3
"""Quarantined, verified, self-rolling-back update of CLIProxyAPI on a server.

Installed on the server as /usr/local/sbin/cliproxy-update and run daily by a root
systemd timer. Standard library only, because the server has no workspace venv.

    cliproxy-update run        # one pass: pick, verify, install, check, roll back on failure
    cliproxy-update status     # current, known-good, quarantine table, blacklist, HALT
    cliproxy-update candidate  # what `run` would install now, and why; changes nothing
    cliproxy-update check      # run the step-6 checks against the running proxy

The project publishes releases more than once a day, with no signature and no
attestation: only a `checksums.txt` uploaded during the build and the sha256 GitHub
computes per asset. So one pass does this, in order, and stops at the first failure:

1. `HALT` in the state directory stops everything until a human removes it.
2. Releases on the pinned major line (`MAJOR`, default 8), not draft, not prerelease,
   not blacklisted, newer than the running version.
3. QUARANTINE. Every release is recorded the first time it is seen, with its asset
   digest. The candidate is the newest one first seen more than `QUARANTINE_H` hours
   ago (default 24) whose digest has not changed since. "The newest release" would
   never have sat a day at this release rate. A release whose digest changed is
   blacklisted: a re-uploaded binary under an old tag is exactly the thing to refuse.
4. Download into a root-only temporary directory. The tarball's sha256 must equal
   BOTH its line in `checksums.txt` and GitHub's digest. The archive must hold
   exactly one executable named `cli-proxy-api`, an ELF file, with no absolute path
   and no `..` anywhere.
5. Stop the service, copy `config.yaml` and the NAMES of the token files (not their
   contents) aside, swap the binary by rename, start the service.
6. Checks, all mandatory: `/healthz`; `/v1/models` 401 without the key and 200 with
   it; a real one-line request through Codex and through Claude; the ZEBRA test on
   the Claude route (a system prompt that must reach the model unmodified); a forced
   tool call on the Claude route in the shape Hindsight sends for structured output.
   Each check spends a few tokens of the subscription.
7. On any failure: restore the previous binary and `config.yaml` (v8 rewrites the
   config on load, and an older binary may not read what a newer one wrote), restart,
   re-check, blacklist the version. Tokens are NOT restored: they may have been
   refreshed meanwhile. If the rollback fails too, write `HALT`.
7a. DEFERRAL. A model check answered HTTP 429 or 529, an error body of type
   `rate_limit_error` or `overloaded_error`, or no answer within `LLM_TIMEOUT_S`,
   says the upstream had no capacity at that minute, not that the binary is broken.
   It is not a failure in the sense of step 7: the new binary is rolled back exactly
   as there, but NOT blacklisted, so the next pass picks it again. A rollback
   re-check that is itself deferred does not write `HALT`: the previous binary is
   running and answered `/healthz` and `/v1/models`, and only the model call went
   unanswered. Events keep their kinds (`update_failed`, `rolled_back`) and carry
   `deferred: true` / `verified: false`, so every reader of `events.jsonl` still sees
   them. `check` reports a deferral the same way and exits 75 (EX_TEMPFAIL).
7b. EXPIRED LOGIN. A model check that answers anything but HTTP 200 with
   `auth_unavailable` in its message says the proxy holds no usable subscription
   login for that provider, which no release can fix and a human must. MEASURED
   2026-10-30 on the memory server: the proxy journal showed `refresh failed with
   status 400`, `invalid_grant`, `Refresh token expired` every 30 minutes from
   2026-10-29 15:30 server time, 56 times, and every Claude call answered HTTP 503
   `auth_unavailable: no auth available (providers=claude, ...`; the nightly pass then
   blacklisted v8.0.47, which was not at fault, and wrote `HALT` after the rollback
   re-check got the same 503. So `run` first checks the RUNNING binary (`checks` with
   `full=False`) before it downloads anything: an expired login writes `AUTH_EXPIRED`,
   logs `auth_expired` and stops with the login command to run, with no download,
   swap, blacklist or `HALT` and no `update_start`; a deferred pre-flight logs
   `update_deferred` and stops the same way; any other pre-flight failure logs
   `preflight_failed` and the update goes ahead, because a broken running binary must
   stay replaceable. After the swap an expired login is handled as a deferral (step
   7a) with `auth_expired: true`, and a rollback re-check that finds it writes
   `AUTH_EXPIRED`, not `HALT`. `AUTH_EXPIRED` is NOT a halt: `run` keeps running each
   night, and the file is removed only where the checks passed (the end of `check`, a
   passing pre-flight, a successful update, a passing rollback re-check). `check`
   reports an expired login with the login command and exits 77 (EX_NOPERM).
8. Keep the three newest releases plus the known-good one on disk.

Nothing here sends a notification. Every event is appended to `events.jsonl` in the
state directory, and the operator's laptop reads that, `HALT` and `AUTH_EXPIRED` (one
line: provider, UTC time, the check's message; present while a subscription login is
expired, step 7b) over ssh and notifies him through his own sink; this server holds
no credential for any channel.

Outbound requests carry the workspace User-Agent from `/usr/local/etc/user-agent.txt`.
Settings come from `/etc/default/cliproxy-update` (KEY=VALUE): `KEY_FILE` (the proxy
client key), `CLAUDE_MODEL`, `CODEX_MODEL`, and optionally `MAJOR`, `QUARANTINE_H`,
`PORT`, `SERVICE`, `REPO`. On a server with no subscription login, set `CLAUDE_MODEL`
and/or `CODEX_MODEL` to `none` to skip their step-6 checks, and set `CHAT_MODEL` to a
model behind an openai-compatibility provider, checked with the ZEBRA prompt on
`/v1/chat/completions`; with both set to `none`, `CHAT_MODEL` is required.
"""
from __future__ import annotations

import grp
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/opt/cliproxyapi")
BIN = BASE / "bin" / "cli-proxy-api"
RELEASES = BASE / "releases"
CONFIG = Path("/etc/cliproxyapi/config.yaml")
AUTH_DIR = Path("/var/lib/cliproxyapi/auth")
STATE_DIR = Path("/var/lib/cliproxy-update")
STATE = STATE_DIR / "state.json"
EVENTS = STATE_DIR / "events.jsonl"
HALT = STATE_DIR / "HALT"
AUTH_EXPIRED = STATE_DIR / "AUTH_EXPIRED"
UA_FILE = Path("/usr/local/etc/user-agent.txt")
SETTINGS_FILE = Path("/etc/default/cliproxy-update")
KEEP_RELEASES = 3
SERVICE_GROUP = "cliproxyapi"
HTTP_TIMEOUT_S = 60
LLM_TIMEOUT_S = 180


class UpdateError(RuntimeError):
    """A step failed; the message says which."""


class CheckDeferred(UpdateError):
    """A model check got no usable answer for a reason that says nothing about the
    binary: rate limit, overload, or timeout (contract step 7a)."""


class CheckAuthExpired(UpdateError):
    """A model check found no usable subscription login behind the proxy: a human must
    log in again, and no release is at fault (contract step 7b)."""

    def __init__(self, check: str, provider: str, message: str):
        super().__init__(f"{check}: the {provider} login of the proxy expired: {message[:200]}")
        self.provider = provider


# MEASURED 2026-10-30 on the server: 503 with the message "auth_unavailable: no auth
# available (providers=claude, model=...". No space after a comma inside the list.
AUTH_PROVIDERS = re.compile(r"providers=([a-z0-9_-]+(?:,[a-z0-9_-]+)*)")
# The second copy of this table is in scripts/server/hindsight-status.py; the two files
# are installed separately, so each carries its own.
LOGIN_COMMANDS = {"claude": "cli-proxy-api -claude-login -no-browser as user cliproxyapi",
                  "codex": "cli-proxy-api -codex-device-login as user cliproxyapi"}


def login_hint(provider: str) -> str:
    names = provider.split(",")
    known = [LOGIN_COMMANDS[n] for n in names if n in LOGIN_COMMANDS]
    if len(known) < len(names):
        known.append("see the proxy login guide")
    return " and ".join(known)


# MEASURED 2026-10-11 on the server: the Claude route answered 429 with error type
# rate_limit_error ("This request would exceed your account's rate limit").
# 529 / overloaded_error is the Anthropic overload answer.
TRANSIENT_HTTP = {429, 529}
TRANSIENT_ERROR_TYPES = {"rate_limit_error", "overloaded_error"}


# ============================================================
# Settings, state, events
# ============================================================

def settings() -> dict:
    out = {"MAJOR": "8", "QUARANTINE_H": "24", "PORT": "8317", "SERVICE": "cliproxyapi",
           "REPO": "router-for-me/CLIProxyAPI"}
    if SETTINGS_FILE.exists():
        for line in SETTINGS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"')
    for need in ("KEY_FILE", "CLAUDE_MODEL", "CODEX_MODEL"):
        if not out.get(need):
            raise UpdateError(f"{need} is not set in {SETTINGS_FILE}")
    if out["CLAUDE_MODEL"] == out["CODEX_MODEL"] == "none" and not out.get("CHAT_MODEL"):
        raise UpdateError(f"CLAUDE_MODEL and CODEX_MODEL are both none and CHAT_MODEL is unset in "
                          f"{SETTINGS_FILE}: no model check would run")
    return out


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"seen": {}, "blacklist": [], "current": None, "known_good": None}


def save_state(state: dict) -> None:
    tmp = STATE.with_name(STATE.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, STATE)


def event(kind: str, **fields) -> None:
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": kind, **fields}
    with EVENTS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(json.dumps(rec, ensure_ascii=False))


def mark_auth_expired(exc: CheckAuthExpired) -> None:
    tmp = AUTH_EXPIRED.with_name(AUTH_EXPIRED.name + ".tmp")
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tmp.write_text(f"{exc.provider} {stamp} {exc}\n", encoding="utf-8")
    os.replace(tmp, AUTH_EXPIRED)


def clear_auth_expired() -> None:
    AUTH_EXPIRED.unlink(missing_ok=True)


def version_key(tag: str) -> tuple[int, ...]:
    return tuple(int(x) for x in tag.lstrip("v").split("."))


# ============================================================
# HTTP
# ============================================================

def _ua() -> str:
    return UA_FILE.read_text(encoding="utf-8").splitlines()[0].strip()


def http_get(url: str) -> bytes:
    if not url.startswith("https://"):
        raise UpdateError(f"refusing a non-https URL: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": _ua(), "Accept": "application/vnd.github+json"})  # noqa: S310 - https checked above
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:  # noqa: S310 - https checked above
        return resp.read()


def local(method: str, path: str, port: str, headers: dict | None = None,
          body: dict | None = None, timeout: int = HTTP_TIMEOUT_S) -> tuple[int, dict | str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,  # noqa: S310 - fixed loopback URL
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed loopback URL
            raw = resp.read().decode("utf-8", "replace")
            code = resp.status
    except urllib.error.HTTPError as exc:
        raw, code = exc.read().decode("utf-8", "replace"), exc.code
    try:
        return code, json.loads(raw)
    except json.JSONDecodeError:
        return code, raw


# ============================================================
# Release selection
# ============================================================

def asset_name(tag: str) -> str:
    return f"CLIProxyAPI_{tag.lstrip('v')}_linux_amd64.tar.gz"


def refresh_seen(state: dict, cfg: dict, now: float) -> list[dict]:
    """Record first sight and digest of every release on the major line; blacklist
    a release whose digest changed. Returns the eligible releases."""
    rels = json.loads(http_get(f"https://api.github.com/repos/{cfg['REPO']}/releases?per_page=40"))
    line = re.compile(rf"^v{re.escape(cfg['MAJOR'])}\.\d+\.\d+$")
    eligible = []
    for r in rels:
        tag = r.get("tag_name", "")
        if r.get("draft") or r.get("prerelease") or not line.match(tag):
            continue
        asset = next((a for a in r.get("assets", []) if a.get("name") == asset_name(tag)), None)
        csum = next((a for a in r.get("assets", []) if a.get("name") == "checksums.txt"), None)
        if not asset or not csum or not str(asset.get("digest", "")).startswith("sha256:"):
            continue
        digest = asset["digest"].split(":", 1)[1]
        seen = state["seen"].setdefault(tag, {"first_seen": now, "digest": digest})
        if seen["digest"] != digest and tag not in state["blacklist"]:
            state["blacklist"].append(tag)
            event("blacklisted", tag=tag, reason="asset digest changed after first sight",
                  was=seen["digest"], now=digest)
        eligible.append({"tag": tag, "digest": digest, "url": asset["browser_download_url"],
                         "checksums_url": csum["browser_download_url"]})
    return eligible


def pick(state: dict, eligible: list[dict], cfg: dict, now: float) -> tuple[dict | None, str]:
    current = state.get("current")
    qsec = float(cfg["QUARANTINE_H"]) * 3600
    ok = []
    for r in eligible:
        tag = r["tag"]
        if tag in state["blacklist"] or (current and version_key(tag) <= version_key(current)):
            continue
        seen = state["seen"][tag]
        if now - seen["first_seen"] < qsec:
            continue
        ok.append(r)
    if not ok:
        return None, f"nothing newer than {current} has sat {cfg['QUARANTINE_H']} h with an unchanged digest"
    best = max(ok, key=lambda r: version_key(r["tag"]))
    return best, f"{best['tag']} first seen {datetime.fromtimestamp(state['seen'][best['tag']]['first_seen'], timezone.utc):%Y-%m-%d %H:%M} UTC"


# ============================================================
# Download and install
# ============================================================

def fetch_verified(rel: dict, workdir: Path) -> Path:
    tarball = workdir / asset_name(rel["tag"])
    tarball.write_bytes(http_get(rel["url"]))
    sums = http_get(rel["checksums_url"]).decode("utf-8", "replace")
    got = hashlib.sha256(tarball.read_bytes()).hexdigest()
    listed = next((ln.split()[0] for ln in sums.splitlines()
                   if ln.strip().endswith(asset_name(rel["tag"]))), None)
    if got != rel["digest"]:
        raise UpdateError(f"sha256 {got} != GitHub digest {rel['digest']}")
    if got != listed:
        raise UpdateError(f"sha256 {got} != checksums.txt {listed}")
    with tarfile.open(tarball) as tf:
        members = tf.getmembers()
        for m in members:
            if m.name.startswith("/") or ".." in Path(m.name).parts:
                raise UpdateError(f"unsafe path in archive: {m.name}")
        exe = [m for m in members if m.isfile() and Path(m.name).name == "cli-proxy-api"]
        if len(exe) != 1 or any(m.isfile() and m.mode & 0o111 and m is not exe[0] for m in members):
            raise UpdateError("archive must hold exactly one executable, cli-proxy-api")
        src = tf.extractfile(exe[0])
        out = workdir / "cli-proxy-api"
        out.write_bytes(src.read())
    if out.read_bytes()[:4] != b"\x7fELF":
        raise UpdateError("cli-proxy-api is not an ELF executable")
    return out


def install_exec(path: Path) -> None:
    """root-owned, executable only by the service's group: the service runs the binary
    and cannot change it."""
    os.chown(path, 0, grp.getgrnam(SERVICE_GROUP).gr_gid)
    os.chmod(path, 0o750)  # noqa: S103  # nosec B103 - a service binary must be executable by its group


def systemctl(*args: str) -> None:
    subprocess.run(["systemctl", *args], check=True, capture_output=True, text=True)


def swap_binary(src: Path) -> None:
    tmp = BIN.with_name(BIN.name + ".new")
    shutil.copyfile(src, tmp)
    install_exec(tmp)
    os.replace(tmp, BIN)


def backup_config(stamp: str) -> Path:
    d = STATE_DIR / "backup" / stamp
    d.mkdir(parents=True, mode=0o700)
    shutil.copy2(CONFIG, d / "config.yaml")
    (d / "auth-files.txt").write_text("\n".join(sorted(p.name for p in AUTH_DIR.iterdir())) + "\n")
    return d


def restore_config(d: Path) -> None:
    shutil.copy2(d / "config.yaml", CONFIG)
    shutil.chown(CONFIG, "cliproxyapi", "cliproxyapi")
    os.chmod(CONFIG, 0o600)


# ============================================================
# Checks
# ============================================================

def llm(name: str, path: str, port: str, headers: dict, body: dict) -> tuple[int, dict | str]:
    """One model call for a check; raises CheckAuthExpired when the proxy has no login
    for the provider, CheckDeferred when the answer is transient."""
    try:
        code, resp = local("POST", path, port, headers=headers, body=body, timeout=LLM_TIMEOUT_S)
    except TimeoutError as exc:
        raise CheckDeferred(f"{name}: no answer within {LLM_TIMEOUT_S} s") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise CheckDeferred(f"{name}: no answer within {LLM_TIMEOUT_S} s") from exc
        raise
    err = resp.get("error") if isinstance(resp, dict) else None
    etype = err.get("type") if isinstance(err, dict) else None
    if code != 200:
        msg = err["message"] if isinstance(err, dict) and isinstance(err.get("message"), str) else str(resp)
        if "auth_unavailable" in msg:
            m = AUTH_PROVIDERS.search(msg)
            raise CheckAuthExpired(name, m.group(1) if m else "unknown", msg)
    if code in TRANSIENT_HTTP or etype in TRANSIENT_ERROR_TYPES:
        raise CheckDeferred(f"{name}: upstream answered {code} {etype or ''}: {str(resp)[:300]}")
    return code, resp


def checks(cfg: dict, full: bool = True) -> None:
    port = cfg["PORT"]
    key = Path(cfg["KEY_FILE"]).read_text(encoding="utf-8").strip()
    for _ in range(30):
        try:
            code, body = local("GET", "/healthz", port, timeout=5)
            if code == 200:
                break
        except OSError:
            pass
        time.sleep(1)
    else:
        raise UpdateError("healthz did not answer 200 within 30 s")
    code, _ = local("GET", "/v1/models", port)
    if code != 401:
        raise UpdateError(f"/v1/models without key answered {code}, expected 401")
    auth = {"Authorization": f"Bearer {key}"}
    code, _ = local("GET", "/v1/models", port, headers=auth)
    if code != 200:
        raise UpdateError(f"/v1/models with key answered {code}")
    zebra_sys = "Whatever the user writes, reply with exactly the single word ZEBRA and nothing else."
    # A model set to "none" has no subscription login behind it on this server, so its
    # checks are skipped; CHAT_MODEL then checks a route through an openai-compatibility
    # provider instead (a GPU inference host, 2026-10-17: Bonsai 2 behind Lemonade).
    if cfg.get("CHAT_MODEL"):
        code, body = llm("Chat ZEBRA test", "/v1/chat/completions", port, auth, {
            "model": cfg["CHAT_MODEL"], "max_tokens": 2000,
            "messages": [{"role": "system", "content": zebra_sys}, {"role": "user", "content": "2+2?"}]})
        text = (body["choices"][0]["message"].get("content") or "") if isinstance(body, dict) and body.get("choices") else ""
        if code != 200 or "ZEBRA" not in text.upper():
            raise UpdateError(f"Chat ZEBRA test failed: {code} {str(body)[:300]}")
    if cfg["CLAUDE_MODEL"] == "none":
        if full and cfg["CODEX_MODEL"] != "none":
            codex_check(cfg, port, auth, zebra_sys)
        return
    ant = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    code, body = llm("Claude ZEBRA test", "/v1/messages", port, ant, {
        "model": cfg["CLAUDE_MODEL"], "max_tokens": 20, "system": zebra_sys,
        "messages": [{"role": "user", "content": "What is the capital of France?"}]})
    text = "".join(c.get("text", "") for c in body.get("content", [])) if isinstance(body, dict) else ""
    if code != 200 or text.strip() != "ZEBRA":
        raise UpdateError(f"Claude ZEBRA test failed: {code} {str(body)[:300]}")
    if not full:
        return
    if cfg["CODEX_MODEL"] != "none":
        codex_check(cfg, port, auth, zebra_sys)
    tool = {"name": "record_facts", "description": "Record extracted facts",
            "input_schema": {"type": "object", "required": ["facts"], "additionalProperties": False,
                             "properties": {"facts": {"type": "array", "items": {"type": "string"}}}}}
    code, body = llm("Claude structured-output test", "/v1/messages", port, ant, {
        "model": cfg["CLAUDE_MODEL"], "max_tokens": 1000, "tools": [tool],
        "tool_choice": {"type": "tool", "name": "record_facts"},
        "messages": [{"role": "user", "content": "Анна переехала в Лиссабон в 2025 году."}]})
    blocks = body.get("content", []) if isinstance(body, dict) else []
    use = next((b for b in blocks if b.get("type") == "tool_use"), None)
    if code != 200 or not use or not isinstance(use.get("input", {}).get("facts"), list):
        raise UpdateError(f"Claude structured-output test failed: {code} {str(body)[:300]}")


def codex_check(cfg: dict, port: str, auth: dict, zebra_sys: str) -> None:
    code, body = llm("Codex test", "/v1/chat/completions", port, auth, {
        "model": cfg["CODEX_MODEL"], "max_completion_tokens": 200,
        "messages": [{"role": "system", "content": zebra_sys}, {"role": "user", "content": "2+2?"}]})
    text = body["choices"][0]["message"]["content"] if isinstance(body, dict) and body.get("choices") else ""
    if code != 200 or text.strip() != "ZEBRA":
        raise UpdateError(f"Codex test failed: {code} {str(body)[:300]}")


# ============================================================
# Commands
# ============================================================

def identify_current(state: dict) -> str | None:
    if state.get("current"):
        return state["current"]
    digest = hashlib.sha256(BIN.read_bytes()).hexdigest()
    for d in RELEASES.iterdir():
        f = d / "cli-proxy-api"
        if f.exists() and hashlib.sha256(f.read_bytes()).hexdigest() == digest:
            return "v" + d.name
    return None


def prune_releases(state: dict) -> None:
    keep = {state.get("current"), state.get("known_good")}
    dirs = sorted((d for d in RELEASES.iterdir() if d.is_dir() and re.match(r"^\d+\.\d+\.\d+$", d.name)),
                  key=lambda d: version_key(d.name))
    keep |= {"v" + d.name for d in dirs[-KEEP_RELEASES:]}
    for d in dirs:
        if "v" + d.name not in keep:
            shutil.rmtree(d)
            event("pruned", tag="v" + d.name)


def cmd_run() -> int:
    STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    if HALT.exists():
        print(f"HALT present ({HALT}); doing nothing")
        return 0
    cfg = settings()
    state = load_state()
    state["current"] = identify_current(state)
    if state["current"] is None:
        # No rollback target: an update now could not be undone, so stop for a human.
        HALT.write_text("the installed binary matches no release in the releases directory; "
                        "refusing to update without a rollback target\n", encoding="utf-8")
        event("halt", reason="current binary unknown")
        return 1
    if not state.get("known_good"):
        state["known_good"] = state["current"]
    now = time.time()
    eligible = refresh_seen(state, cfg, now)
    rel, why = pick(state, eligible, cfg, now)
    save_state(state)
    if not rel:
        print(why)
        return 0
    # Pre-flight against the running binary (step 7b): an expired login or a transient
    # answer stops here, before anything is downloaded or swapped.
    try:
        checks(cfg, full=False)
    except CheckAuthExpired as exc:
        mark_auth_expired(exc)
        event("auth_expired", provider=exc.provider, reason=str(exc)[:500])
        print(f"the {exc.provider} login of the proxy expired: log in again "
              f"({login_hint(exc.provider)}); no update attempted")
        return 1
    except CheckDeferred as exc:
        event("update_deferred", reason=str(exc)[:500])
        return 1
    except (UpdateError, OSError, subprocess.CalledProcessError, KeyError, TypeError) as exc:
        event("preflight_failed", reason=str(exc)[:500])
    else:
        clear_auth_expired()
    prev, stamp = state["current"], datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    event("update_start", tag=rel["tag"], previous=prev, why=why)
    with tempfile.TemporaryDirectory(dir=STATE_DIR) as tmp:
        try:
            new_bin = fetch_verified(rel, Path(tmp))
        except (UpdateError, OSError, tarfile.TarError) as exc:
            state["blacklist"].append(rel["tag"])
            save_state(state)
            event("download_refused", tag=rel["tag"], reason=str(exc))
            return 1
        rdir = RELEASES / rel["tag"].lstrip("v")
        rdir.mkdir(mode=0o755, exist_ok=True)
        shutil.copyfile(new_bin, rdir / "cli-proxy-api")
        install_exec(rdir / "cli-proxy-api")
    backup = backup_config(stamp)
    try:
        systemctl("stop", cfg["SERVICE"])
        swap_binary(rdir / "cli-proxy-api")
        systemctl("start", cfg["SERVICE"])
        checks(cfg)
    except (UpdateError, OSError, subprocess.CalledProcessError, KeyError, TypeError) as exc:
        deferred = isinstance(exc, (CheckDeferred, CheckAuthExpired))
        # A deferred check still rolls back (step 7a). Keeping the new binary because
        # every check that did answer passed would put into service a version that never
        # passed the ZEBRA or the structured-output test, the two checks that exist
        # because a proxy release can mangle the system prompt or a tool call while
        # /healthz and /v1/models stay green. The previous binary is the known-good
        # state; the only cost of rolling back is one more swap on the next pass.
        # An expired login (step 7b) is no more the release's fault than a rate limit.
        if isinstance(exc, CheckAuthExpired):
            mark_auth_expired(exc)
            event("update_failed", tag=rel["tag"], reason=str(exc)[:500], deferred=True, auth_expired=True)
        elif deferred:
            event("update_failed", tag=rel["tag"], reason=str(exc)[:500], deferred=True)
        else:
            event("update_failed", tag=rel["tag"], reason=str(exc)[:500])
            state["blacklist"].append(rel["tag"])
            save_state(state)
        try:
            systemctl("stop", cfg["SERVICE"])
            swap_binary(RELEASES / prev.lstrip("v") / "cli-proxy-api")
            restore_config(backup)
            systemctl("start", cfg["SERVICE"])
            checks(cfg, full=False)
            clear_auth_expired()
            event("rolled_back", tag=prev)
        except CheckAuthExpired as exc2:
            mark_auth_expired(exc2)
            event("rolled_back", tag=prev, verified=False, auth_expired=True, reason=str(exc2)[:500])
        except CheckDeferred as exc2:
            event("rolled_back", tag=prev, verified=False, reason=str(exc2)[:500])
        except (UpdateError, OSError, subprocess.CalledProcessError, KeyError, TypeError) as exc2:
            HALT.write_text(f"rollback to {prev} failed: {exc2}\n", encoding="utf-8")
            event("halt", reason=f"rollback failed: {exc2}"[:500])
        return 1
    clear_auth_expired()
    state["current"] = state["known_good"] = rel["tag"]
    save_state(state)
    event("updated", tag=rel["tag"], previous=prev)
    prune_releases(state)
    return 0


def cmd_candidate() -> int:
    cfg, state = settings(), load_state()
    state["current"] = identify_current(state)
    now = time.time()
    eligible = refresh_seen(state, cfg, now)
    rel, why = pick(state, eligible, cfg, now)
    print(f"current {state['current']}; candidate: {rel['tag'] if rel else 'none'} ({why})")
    return 0


def cmd_check() -> int:
    try:
        checks(settings())
    except CheckAuthExpired as exc:
        mark_auth_expired(exc)
        event("error", reason=str(exc)[:500], auth_expired=True)
        print(f"the {exc.provider} login of the proxy expired: log in again "
              f"({login_hint(exc.provider)})", file=sys.stderr)
        return 77
    except CheckDeferred as exc:
        print(f"checks deferred, not failed: {exc}", file=sys.stderr)
        event("error", reason=str(exc)[:500], deferred=True)
        return 75
    clear_auth_expired()
    print("all checks passed")
    return 0


def cmd_status() -> int:
    state = load_state()
    print(f"current    {state.get('current')}\nknown_good {state.get('known_good')}")
    print(f"HALT       {'PRESENT: ' + HALT.read_text().strip() if HALT.exists() else 'no'}")
    print(f"blacklist  {', '.join(state.get('blacklist', [])) or '-'}")
    for tag, s in sorted(state.get("seen", {}).items(), key=lambda kv: version_key(kv[0]))[-8:]:
        age = (time.time() - s["first_seen"]) / 3600
        print(f"  {tag:10} seen {age:6.1f} h ago  {s['digest'][:12]}")
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    fn = {"run": cmd_run, "status": cmd_status, "candidate": cmd_candidate, "check": cmd_check}.get(cmd)
    if not fn:
        print(__doc__)
        return 2
    try:
        return fn()
    except (UpdateError, OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        print(f"cliproxy-update: {exc}", file=sys.stderr)
        try:
            event("error", reason=str(exc)[:500])
        except OSError as log_exc:
            print(f"cliproxy-update: could not record the event: {log_exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
