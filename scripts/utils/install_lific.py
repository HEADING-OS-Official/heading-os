#!/usr/bin/env python3
"""Lific, the issue tracker, installed and wired by `scripts/heading-install.py` (HOS-306).

Usage (from the installer, never by hand):
    from scripts.utils import install_lific as lific
    lific.check_binary(ctx); lific.run_binary(ctx)       # and init, service, project, mcp

Five steps, each a check that reads the real state and a run that repairs it
(deployment design section 2 step 6, registry name `plans`, 2026-11-01; audit
A6). What they leave, relative to the executive's home:

    ~/.local/bin/lific                 the pinned release, checksum-verified
    ~/.config/lific/lific.toml         `lific init --no-service`, loopback only
    <data repository>/<runtime store>/lific/lific.db
                                       the database, where the registry row
                                       `lific` (runtime services) places it
    lific.service                      the service layer's family `lific`
    project HOS, bot user `claude`     maintainer, with one API key
    ~/.config/lific/agent-token        that key, mode 0600, nowhere else
    ~/.local/bin/lific-mcp             the launcher, rendered from
                                       scripts/templates/lific-mcp-launcher.sh
    Claude Code user config            `lific` registered over stdio

Never `lific connect`: it rewrites the whole Claude Code user config and writes
a key into it in plain text (.claude/rules/lific.md item 1). The project key is
always HOS, because .claude/rules/lific.md names HOS and the installer follows
the rule, not the other way round (audit A6 inconsistency).

Secrets: the admin and bot passwords are generated here and handed to Lific on
stdin, never on a command line; the admin password is kept in the engine's
`.env` as LIFIC_ADMIN_PASSWORD (mode 600, through scripts/utils/env_file.py),
the bot password nowhere, the bot key only in the token file. No output of a
command that carries a secret is ever printed.

Every function takes its home directory, its `systemctl` and its `lific`
binary from the step context (`ctx.home`, `ctx.systemctl`, `ctx.lific_bin`,
each optional), so a proof run points all three at scratch. Standard library
only at import time, like the installer. Linux (systemd user units) is the
tested path; macOS (launchd, through the service layer) is written and
UNTESTED on a Mac.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import platform
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.request
from pathlib import Path

from scripts.utils.install_runner import changed_since_start, runner_env, runner_problem, service_pid

VERSION = "2.11.1"
RELEASE = f"https://github.com/VoidNullable/lific/releases/download/v{VERSION}/"
# Copied from the release's own sha256sums.txt, MEASURED 2026-11-01 (the
# linux-x86_64 sum equals the one docs/LIFIC.md section 8 recorded at upgrade).
# Pinned here rather than read from the release at install time, so an asset
# and checksum file replaced later on GitHub together are still refused.
SHA256 = {
    "lific-linux-x86_64": "87b20621ce7e075db1548200291372f6d2ca8255fe3d78a555a99b6c31180aed",  # pragma: allowlist secret (a published checksum)
    "lific-linux-aarch64": "b79d946c353ca19fa49347da473d52234f0a19fa18973c0bda91af37f38aceda",  # pragma: allowlist secret (a published checksum)
    "lific-macos-x86_64": "88033d622dc7367e26221e208d31e7fd8be412fb546eae740994ccfab53bf084",  # pragma: allowlist secret (a published checksum)
    "lific-macos-aarch64": "fa13e48ae446c1a87dfd4423b167e8e9f65e55638e4e88c8544d0548eb89b835",  # pragma: allowlist secret (a published checksum)
}
PROJECT = "HOS"
PROJECT_NAME = "HEADING OS"
ADMIN = "admin"
BOT = "claude"
KEY_NAME = "claude-code"
UNIT = "lific.service"
LAUNCHD_LABEL = "com.headingos.lific"
# A lific.service without the service layer's marker was written by someone
# else (this machine's own `lific init`), and is adopted.
from scripts.utils.schedule import written_by_service_layer  # noqa: E402
ENGINE_ROOT = Path(__file__).resolve().parent.parent.parent
LAUNCHER_TEMPLATE = ENGINE_ROOT / "scripts" / "templates" / "lific-mcp-launcher.sh"


class LificError(RuntimeError):
    """A plain sentence for the installer to show; never carries a secret."""


# ============================================================
# Where things are
# ============================================================

def binary_path(home: Path) -> Path:
    return home / ".local" / "bin" / "lific"


def config_path(home: Path) -> Path:
    return home / ".config" / "lific" / "lific.toml"


def token_path(home: Path) -> Path:
    return home / ".config" / "lific" / "agent-token"


def launcher_path(home: Path) -> Path:
    return home / ".local" / "bin" / "lific-mcp"


def unit_path(home: Path) -> Path:
    return home / ".config" / "systemd" / "user" / UNIT


def claude_config_path(home: Path) -> Path:
    """The Claude Code user config `claude mcp add --scope user` writes to."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base).expanduser() if base else home) / ".claude.json"


def _home(ctx) -> Path:
    return Path(getattr(ctx, "home", None) or Path.home())


def _lific(ctx) -> Path:
    return Path(getattr(ctx, "lific_bin", None) or binary_path(_home(ctx)))


def _systemctl(ctx) -> str:
    return str(getattr(ctx, "systemctl", None) or "systemctl")


def _env(ctx) -> dict[str, str]:
    """The installer's child environment, with HOME pointed at this context's home.

    An injected `ctx.systemctl` puts its directory first on PATH, so every
    service-manager tool a child resolves by name (the service layer's
    `systemctl`, `loginctl`, `launchctl`) comes from there, never from the
    machine's own service manager.
    """
    env = ctx.env()
    env["HOME"] = str(_home(ctx))
    runner_env(env, getattr(ctx, "systemctl", None))
    env.pop("LIFIC_TOKEN", None)  # an inherited token would make admin commands run as someone else
    return env


def _cli(ctx, *args: str, stdin: str | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
    """`lific --config <ours> <args>`, output captured and returned, never printed here."""
    return subprocess.run([str(_lific(ctx)), "--config", str(config_path(_home(ctx))), *args],
                          input=stdin, capture_output=True, text=True, errors="replace",
                          env=_env(ctx), cwd=str(_home(ctx)), timeout=timeout)


def _cli_json(ctx, *args: str):
    proc = _cli(ctx, *args, "--json")
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return None


def _why(proc: subprocess.CompletedProcess) -> str:
    """The last stderr line only: stdout of these commands can hold a key."""
    lines = (proc.stderr or "").strip().splitlines()
    return lines[-1][-300:] if lines else f"exit {proc.returncode}"


def _write_private(path: Path, text: str, mode: int = 0o600) -> None:
    """Atomically, and never readable by others for an instant (created at `mode`)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


# ============================================================
# Step lific-binary
# ============================================================

def asset_name() -> str:
    system, machine = platform.system(), platform.machine().lower()
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(machine)
    osname = {"Linux": "linux", "Darwin": "macos"}.get(system)
    name = f"lific-{osname}-{arch}"
    if name not in SHA256:
        raise LificError(f"Lific {VERSION} publishes no build for {system} {machine}")
    return name


def installed_version(binary: Path) -> tuple[int, ...] | None:
    if not (binary.is_file() and os.access(binary, os.X_OK)):
        return None
    try:
        out = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"lific (\d+)\.(\d+)\.(\d+)", out)
    return tuple(int(x) for x in m.groups()) if m else None


def _pinned() -> tuple[int, ...]:
    return tuple(int(x) for x in VERSION.split("."))


def check_binary(ctx) -> bool:
    """The pinned version or newer: a newer one is kept, never downgraded under its database."""
    found = installed_version(_lific(ctx))
    return found is not None and found >= _pinned()


def run_binary(ctx) -> None:
    name = asset_name()
    target = _lific(ctx)
    old = installed_version(target)
    sys.path.insert(0, str(ENGINE_ROOT))
    from scripts.utils.user_agent import user_agent  # stdlib only; the one workspace constant

    url = RELEASE + name
    req = urllib.request.Request(url, headers={"User-Agent": user_agent()})  # noqa: S310 - fixed https URL
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as tmp:
        dl = Path(tmp) / name
        with urllib.request.urlopen(req, timeout=300) as resp, dl.open("wb") as out:  # noqa: S310 - fixed https URL
            shutil.copyfileobj(resp, out)
        digest = hashlib.sha256(dl.read_bytes()).hexdigest()
        if digest != SHA256[name]:
            raise LificError(f"{name} does not match the checksum Lific {VERSION} published; nothing was installed")
        os.chmod(dl, 0o700)
        if old is not None:
            # Kept beside it, the way this workspace upgrades: a move, so a running
            # service keeps its open file until it restarts.
            os.replace(target, target.with_name("lific-" + ".".join(map(str, old))))
        os.replace(dl, target)
    if old is not None:
        print(f"  Lific {'.'.join(map(str, old))} was replaced by {VERSION} (old binary kept beside it); "
              "a running service uses the new one after its next restart")


# ============================================================
# Step lific-init
# ============================================================

def database_path(ctx) -> Path:
    """Where the registry of the executive's data repository puts the database.

    The registry row `lific` (runtime services) places the tracker's files
    directly in its service folder, not under a kind; that folder is the parent
    of `runtime:lific/state`, asked of the data repository's own registry
    through the engine's resolver, never spelled here.
    """
    proc = subprocess.run([str(ctx.python), str(ctx.engine / "scripts" / "data-path.py"),
                           "--data-root", str(ctx.data), "path", "runtime:lific/state"],
                          capture_output=True, text=True, errors="replace", env=ctx.env(),
                          cwd=str(ctx.engine), timeout=60)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise LificError(f"the data repository cannot place the Lific database: {_why(proc)}")
    return Path(proc.stdout.strip().splitlines()[-1]).parent / "lific.db"


def read_config(home: Path) -> dict | None:
    try:
        return tomllib.loads(config_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _table(conf: dict | None, name: str) -> dict | None:
    """The `[name]` table of a lific.toml, {} when absent; None when the key holds something else."""
    value = (conf or {}).get(name, {})
    return value if isinstance(value, dict) else None


_TABLES = ("server", "auth", "database")


def _config_ok(conf: dict | None, db: Path) -> bool:
    if not conf:
        return False
    server, auth, database = (_table(conf, t) for t in _TABLES)
    if server is None or auth is None or database is None:
        return False  # a scalar where a table belongs: an invalid config, repaired by run_init
    return (server.get("host") == "127.0.0.1" and auth.get("allow_signup") is False
            and auth.get("required") is True and database.get("path") == str(db))


def _admin_exists(ctx) -> bool:
    users = _cli_json(ctx, "user", "list")
    return isinstance(users, list) and any(
        isinstance(u, dict) and u.get("username") == ADMIN and u.get("is_admin") for u in users)


def check_init(ctx) -> bool:
    try:
        db = database_path(ctx)
        return (_config_ok(read_config(_home(ctx)), db) and db.is_file() and check_binary(ctx)
                and _admin_exists(ctx))
    except (LificError, OSError, subprocess.SubprocessError):
        return False


def _set_key(text: str, section: str, key: str, literal: str) -> str:
    """`key = literal` inside `[section]`, replaced in place or added at the section's end."""
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.strip() == f"[{section}]"), None)
    if start is None:
        return text.rstrip("\n") + f"\n\n[{section}]\n{key} = {literal}\n"
    end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    for i in range(start + 1, end):
        if re.match(rf"\s*{re.escape(key)}\s*=", lines[i]):
            lines[i] = f"{key} = {literal}"
            break
    else:
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        lines.insert(end, f"{key} = {literal}")
    return "\n".join(lines) + "\n"


def run_init(ctx) -> None:
    home, db = _home(ctx), database_path(ctx)
    conf = read_config(home)
    configured = (_table(conf, "database") or {}).get("path")
    if configured and configured != str(db) and Path(configured).exists():
        raise LificError(f"{config_path(home)} already points at another Lific database ({configured}); "
                         f"move that database to {db} or remove the file, then run this again")
    config_path(home).parent.mkdir(parents=True, exist_ok=True)
    db.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(db.parent, 0o700)
    args = ["init", "--no-service", "--auth-mode", "passwords", "--name", ADMIN, "--db", str(db), "--json"]
    password = None
    if not (db.is_file() and _admin_exists(ctx)):
        # Kept BEFORE Lific sees it, so a failure after init cannot lose it.
        password = secrets.token_urlsafe(24)
        sys.path.insert(0, str(ENGINE_ROOT))
        from scripts.utils.env_file import upsert_env_value

        upsert_env_value(ctx.engine / ".env", "LIFIC_ADMIN_PASSWORD", password)
    proc = _cli(ctx, *args, stdin=(password + "\n") if password else "")
    if proc.returncode != 0:
        raise LificError(f"`lific init` failed: {_why(proc)}")
    # Lific's defaults for passwords mode bind every interface and allow sign-up
    # (MEASURED 2026-11-01 on v2.11.1 in scratch): loopback only, no sign-up.
    text = config_path(home).read_text(encoding="utf-8")
    # A top-level `server = "..."` (or auth, database) would clash with the
    # [server] table written below, so a key holding no table goes first.
    after = read_config(home)
    wrong = [t for t in _TABLES if after is not None and _table(after, t) is None]
    if wrong:
        lines = text.splitlines()
        first = next((i for i, ln in enumerate(lines) if ln.lstrip().startswith("[")), len(lines))
        text = "\n".join(ln for i, ln in enumerate(lines)
                         if not (i < first and re.match(rf"\s*({'|'.join(wrong)})\s*=", ln))) + "\n"
    for section, key, literal in (("server", "host", '"127.0.0.1"'), ("auth", "allow_signup", "false"),
                                  ("auth", "required", "true"), ("database", "path", json.dumps(str(db)))):
        text = _set_key(text, section, key, literal)
    _write_private(config_path(home), text)


# ============================================================
# Step lific-service
# ============================================================

def _base_url(home: Path) -> str | None:
    server = _table(read_config(home), "server") or {}
    host = server.get("host") or "127.0.0.1"
    if host in (str(ipaddress.IPv4Address(0)), str(ipaddress.IPv6Address(0))):
        host = "127.0.0.1"  # a wildcard bind is reached on loopback
    port = server.get("port")
    return f"http://{host}:{port}" if isinstance(port, int) else None


def health_url(home: Path) -> str | None:
    base = _base_url(home)
    return f"{base}/api/health" if base else None


def _get(url: str, headers: dict[str, str] | None = None) -> tuple[int, bytes] | None:
    """(status, first 4 KB) of a GET to our own loopback server, or None when it does not answer."""
    sys.path.insert(0, str(ENGINE_ROOT))
    from scripts.utils.user_agent import user_agent

    req = urllib.request.Request(url, headers={"User-Agent": user_agent(), **(headers or {})})  # noqa: S310 - http to loopback, from our own config
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 - http to loopback, from our own config
            return resp.status, resp.read(4096)
    except (OSError, ValueError):
        return None


def server_answers(home: Path) -> bool:
    """A real request: Lific's health endpoint answers `ok` (MEASURED 2026-11-01, v2.11.1)."""
    url = health_url(home)
    got = _get(url) if url else None
    return got is not None and got[0] == 200 and got[1].strip() == b"ok"


def unit_adopted(home: Path) -> bool:
    """A lific.service the service layer did not write: someone else's, left untouched."""
    path = unit_path(home)
    return path.exists() and not written_by_service_layer(path)


def _service_running(ctx) -> bool:
    if platform.system() == "Darwin":  # UNTESTED on a Mac
        return subprocess.run(["launchctl", "list", LAUNCHD_LABEL], capture_output=True,
                              env=_env(ctx)).returncode == 0
    sc = _systemctl(ctx)
    enabled = subprocess.run([sc, "--user", "is-enabled", UNIT], capture_output=True, text=True, env=_env(ctx))
    active = subprocess.run([sc, "--user", "is-active", UNIT], capture_output=True, text=True, env=_env(ctx))
    return enabled.stdout.strip() == "enabled" and active.stdout.strip() == "active"


def _server_pid(ctx) -> int | None:
    """The running server's process id, from the service manager."""
    return service_pid(_systemctl(ctx), UNIT, LAUNCHD_LABEL, _env(ctx))


def config_newer_than_server(ctx) -> bool:
    """True when lific.toml changed after the running server started, so it runs on old settings."""
    return changed_since_start(config_path(_home(ctx)), _server_pid(ctx))


def check_service(ctx) -> bool:
    try:
        return (_service_running(ctx) and server_answers(_home(ctx))
                and not config_newer_than_server(ctx))
    except (OSError, subprocess.SubprocessError):
        return False


def _require_runner_complete(ctx) -> None:
    """An injected systemctl must bring every tool the service layer calls, or the machine's own answers."""
    problem = runner_problem(getattr(ctx, "systemctl", None))
    if problem:
        raise LificError(problem)


def _restart(ctx) -> None:
    """Restart the service layer's own lific family, so a repaired lific.toml reaches the server."""
    if platform.system() == "Darwin":  # UNTESTED on a Mac
        cmd = ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"]
    else:
        cmd = [_systemctl(ctx), "--user", "restart", UNIT]
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", env=_env(ctx))
    if proc.returncode != 0:
        raise LificError(f"lific could not be restarted to apply its settings: {_why(proc)}")


def run_service(ctx) -> None:
    home = _home(ctx)
    _require_runner_complete(ctx)
    if platform.system() != "Darwin" and unit_adopted(home):
        print(f"  adopted: {unit_path(home)} was not written by HEADING OS and is left as it is")
        if not check_service(ctx):
            raise LificError(f"the existing {UNIT} is not running, does not answer, or runs on settings older "
                             f"than {config_path(home)}; restart it with: systemctl --user restart {UNIT}")
        return
    proc = subprocess.run([str(ctx.python), str(ctx.engine / "scripts" / "heading-services.py"), "install", "lific"],
                          capture_output=True, text=True, errors="replace", env=_env(ctx), cwd=str(ctx.engine))
    if proc.returncode != 0:
        tail = "\n".join(((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()[-8:])
        raise LificError(f"the service layer could not install lific: {tail}")
    if config_newer_than_server(ctx):
        _restart(ctx)
    for _ in range(30):  # the server needs a moment after start
        if server_answers(home):
            return
        time.sleep(1)
    raise LificError(f"lific runs but does not answer at {health_url(home)}")


# ============================================================
# Step lific-project
# ============================================================

def _mcp_sees_project(ctx, token: str) -> bool:
    """`lific mcp` started with the token lists project HOS: the key is valid and its user may see it.

    `lific mcp` refuses to start on an invalid or revoked token (MEASURED
    2026-11-01, v2.11.1), so a stale token file reads as not in place.
    """
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "heading-install", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "list_resources", "arguments": {"resource_type": "project"}}},
    ]
    env = _env(ctx)
    env["LIFIC_TOKEN"] = token
    try:
        proc = subprocess.run([str(_lific(ctx)), "mcp", "--config", str(config_path(_home(ctx)))],
                              input="".join(json.dumps(x) + "\n" for x in lines), capture_output=True,
                              text=True, errors="replace", env=env, cwd=str(_home(ctx)), timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    for line in proc.stdout.splitlines():
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("id") == 2:
            texts = [c.get("text", "") for c in (msg.get("result") or {}).get("content", []) if isinstance(c, dict)]
            return any(re.search(rf"^- {PROJECT} \|", t, re.M) for t in texts)
    return False


def _bot_is_member(ctx) -> bool:
    users = _cli_json(ctx, "user", "list")
    members = _cli_json(ctx, "member", "list", "--project", PROJECT)
    bot_ok = isinstance(users, list) and any(
        isinstance(u, dict) and u.get("username") == BOT and u.get("is_bot") for u in users)
    return bot_ok and isinstance(members, list) and any(
        isinstance(m, dict) and m.get("username") == BOT and m.get("role") == "maintainer" for m in members)


def _token(home: Path) -> str | None:
    path = token_path(home)
    try:
        if path.stat().st_mode & 0o077:
            return None
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def token_is_bots(home: Path, token: str) -> bool:
    """The key authenticates as the bot and nobody else: `/api/auth/me` with it as a Bearer token.

    MEASURED 2026-11-01 on v2.11.1 in scratch: that endpoint answers the key's
    own user (`username`, `is_admin`). Listing HOS alone would also pass with an
    admin's key, and MCP would then run as the admin.
    """
    base = _base_url(home)
    got = _get(f"{base}/api/auth/me", {"Authorization": f"Bearer {token}"}) if base else None
    if got is None or got[0] != 200:
        return False
    try:
        me = json.loads(got[1])
    except ValueError:
        return False
    return isinstance(me, dict) and me.get("username") == BOT and me.get("is_admin") is False


def _token_ok(ctx, token: str | None) -> bool:
    return bool(token) and token_is_bots(_home(ctx), token) and _mcp_sees_project(ctx, token)


def _bot_user_set(ctx) -> bool:
    """`LIFIC_BOT_USER` in the engine's .env names the bot, the author the [dossier] journal posts as."""
    sys.path.insert(0, str(ENGINE_ROOT))
    from scripts.utils.paths import read_env_value

    return read_env_value(ctx.engine / ".env", "LIFIC_BOT_USER") == BOT


def check_project(ctx) -> bool:
    try:
        return _bot_is_member(ctx) and _token_ok(ctx, _token(_home(ctx))) and _bot_user_set(ctx)
    except (OSError, subprocess.SubprocessError):
        return False


def run_project(ctx) -> None:
    home = _home(ctx)
    projects = _cli_json(ctx, "project", "list")
    if projects is None:
        raise LificError("Lific cannot list its projects; run this again after the lific-init step")
    if not any(isinstance(p, dict) and p.get("identifier") == PROJECT for p in projects):
        proc = _cli(ctx, "project", "create", "--name", PROJECT_NAME, "--identifier", PROJECT,
                    "--description", "The work Claude and you track together", "--json")
        if proc.returncode != 0:
            raise LificError(f"project {PROJECT} could not be created: {_why(proc)}")
    users = _cli_json(ctx, "user", "list") or []
    if not any(isinstance(u, dict) and u.get("username") == BOT for u in users):
        # The bot signs in with its key only; its password is used once, here, and kept nowhere.
        proc = _cli(ctx, "user", "create", "--username", BOT, "--email", f"{BOT}@example.invalid", "--bot",
                    "--json", stdin=secrets.token_urlsafe(24) + "\n")
        if proc.returncode != 0:
            raise LificError(f"the bot user {BOT} could not be created: {_why(proc)}")
    # The [dossier] journal's author (scripts/utils/dossier.py BOT_ENV); doctor requires it.
    sys.path.insert(0, str(ENGINE_ROOT))
    from scripts.utils.env_file import upsert_env_value

    upsert_env_value(ctx.engine / ".env", "LIFIC_BOT_USER", BOT)
    members = _cli_json(ctx, "member", "list", "--project", PROJECT) or []
    mine = next((m for m in members if isinstance(m, dict) and m.get("username") == BOT), None)
    if mine is None:
        proc = _cli(ctx, "member", "add", "--project", PROJECT, "--user", BOT, "--role", "maintainer", "--json")
    elif mine.get("role") != "maintainer":
        proc = _cli(ctx, "member", "role", "--project", PROJECT, "--user", BOT, "--role", "maintainer", "--json")
    else:
        proc = None
    if proc is not None and proc.returncode != 0:
        raise LificError(f"the bot could not join project {PROJECT}: {_why(proc)}")
    if not server_answers(home):
        raise LificError("Lific's server does not answer, so the bot's key cannot be checked; "
                         "the lific-service step comes first")
    if _token_ok(ctx, _token(home)):
        return
    keys = _cli_json(ctx, "key", "list") or []
    active = any(isinstance(k, dict) and k.get("name") == KEY_NAME and not k.get("revoked") for k in keys)
    if active:
        proc = _cli(ctx, "key", "rotate", "--name", KEY_NAME, "--json")
    else:
        proc = _cli(ctx, "key", "create", "--name", KEY_NAME, "--user", BOT, "--json")
    if proc.returncode != 0:
        raise LificError(f"the bot's API key could not be issued: {_why(proc)}")
    try:
        key = json.loads(proc.stdout).get("key")
    except (ValueError, AttributeError):
        key = None
    if not isinstance(key, str) or not key:
        raise LificError("Lific issued no key in its answer")
    _write_private(token_path(home), key + "\n")
    assign = _cli(ctx, "key", "assign", "--name", KEY_NAME, "--user", BOT, "--json")
    if assign.returncode != 0:
        raise LificError(f"the key could not be bound to the bot: {_why(assign)}")
    if not token_is_bots(home, key):
        raise LificError(f"the issued key does not authenticate as the bot {BOT}")


# ============================================================
# Step lific-mcp
# ============================================================

def launcher_text(home: Path, lific_bin: Path) -> str:
    text = LAUNCHER_TEMPLATE.read_text(encoding="utf-8")
    for token, value in (("LIFIC_BIN", lific_bin), ("LIFIC_CONFIG", config_path(home)),
                         ("TOKEN_FILE", token_path(home))):
        text = text.replace("{{" + token + "}}", shlex.quote(str(value)))
    if "{{" in text:
        raise LificError(f"{LAUNCHER_TEMPLATE.name}: a placeholder survived the render")
    return text


def _registered_entry(home: Path) -> dict | None:
    try:
        data = json.loads(claude_config_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = (data.get("mcpServers") or {}).get("lific") if isinstance(data, dict) else None
    return entry if isinstance(entry, dict) else None


def registered_ours(home: Path, launcher: Path) -> bool:
    """The user-scope `lific` entry is exactly what `run_mcp` writes: the launcher, with no arguments."""
    entry = _registered_entry(home)
    return entry is not None and entry.get("command") == str(launcher) and entry.get("args", []) == []


def registered_command(home: Path) -> str | None:
    """The command of the user-scope `lific` MCP server in the Claude Code user config, or None."""
    entry = _registered_entry(home)
    return entry.get("command") if entry else None


def check_mcp(ctx) -> bool:
    home = _home(ctx)
    launcher = launcher_path(home)
    try:
        current = launcher.read_text(encoding="utf-8")
        return (os.access(launcher, os.X_OK) and current == launcher_text(home, _lific(ctx))
                and registered_ours(home, launcher))
    except (OSError, LificError):
        return False


def run_mcp(ctx) -> None:
    home = _home(ctx)
    launcher = launcher_path(home)
    _write_private(launcher, launcher_text(home, _lific(ctx)), mode=0o700)
    if registered_ours(home, launcher):
        return
    claude = shutil.which("claude", path=_env(ctx)["PATH"])
    if not claude:
        raise LificError("Claude Code (`claude`) is not on PATH, so Lific cannot be registered with it")
    if registered_command(home) is not None:
        # Someone else's `lific` entry at user scope; replaced by ours, never by `lific connect`.
        subprocess.run([claude, "mcp", "remove", "--scope", "user", "lific"], capture_output=True,
                       env=_env(ctx), cwd=str(home))
    proc = subprocess.run([claude, "mcp", "add", "--scope", "user", "--transport", "stdio", "lific", "--",
                           str(launcher)], capture_output=True, text=True, errors="replace",
                          env=_env(ctx), cwd=str(home))
    if proc.returncode != 0:
        raise LificError(f"`claude mcp add` failed: {_why(proc)}")
