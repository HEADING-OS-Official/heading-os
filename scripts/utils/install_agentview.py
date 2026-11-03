#!/usr/bin/env python3
"""AgentView and the Claude Code plugins, installed and wired by `scripts/heading-install.py` (HOS-308).

Usage (from the installer, never by hand):
    from scripts.utils import install_agentview as agentview
    agentview.check_agent_watch(ctx); agentview.run_agent_watch(ctx)   # and plugins, agentsview, usage-dashboard

Four steps, each a check that reads the real state and a run that repairs it
(deployment design section 2 step 6, registry name `plans`, 2026-11-01; audit
A9 and B6; the guide AGENTVIEW.md under registry name `official-docs`, parts 1
to 3). What they leave, relative to the executive's home:

    agent-watch@heading-os-mods        enabled for the user, read from
                                       <engine>/scripts/claude-mods/agent-watch
    every `true` key of enabledPlugins in <engine>/.claude/settings.json,
                                       installed by `claude plugin install`
    ~/.local/share/heading-os/agentsview/v0.46.1/agentsview
                                       AgentsView, checksum-verified
    agentsview.service                 the service layer's family `agentsview`,
                                       loopback 127.0.0.1:8080, telemetry off
    proxy-usage-dashboard.timer        the service layer's family of that name

A unit file of the same name that does not carry the service layer's marker
(`SERVICE_LAYER_MARKER` of scripts/utils/schedule.py) was written by someone
else: it is adopted, never rewritten, enabled or restarted, and the step only
checks that it works. The adopt test of `agentsview` is the service layer's own
(`_adopted_units`, the family `adopts_foreign`); the proxy-usage-dashboard
family does not adopt yet (HOS-317), so its step tests its units itself first.

Every function takes its home directory, its `systemctl`, its `claude`, its
`agentsview` and its HTTP getter from the step context (`ctx.home`,
`ctx.systemctl`, `ctx.claude`, `ctx.agentsview`, `ctx.http_get`, each
optional), so a proof run points all of them at scratch. An injected
`systemctl` reaches every child through scripts/utils/install_runner.py. Standard
library only at import time, like the installer. Linux is the tested path;
macOS (launchd, through the service layer) is written and UNTESTED on a Mac.
"""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ENGINE_ROOT))
from scripts.utils.install_runner import runner_env, runner_problem  # noqa: E402 - stdlib only

MARKETPLACE = "heading-os-mods"
AGENT_WATCH = f"agent-watch@{MARKETPLACE}"
# The source `claude plugin marketplace add` takes for the official catalogue,
# READ 2026-11-01 from `claude plugin marketplace list --json` on Claude Code
# 2.1.312: {"name": "claude-plugins-official", "source": "github",
# "repo": "anthropics/claude-plugins-official", "installLocation": ...}.
MARKETPLACE_SOURCES = {"claude-plugins-official": "anthropics/claude-plugins-official"}

VERSION = "0.46.1"
RELEASE = f"https://github.com/kenn-io/agentsview/releases/download/v{VERSION}/"
# Repository and asset names READ 2026-11-01 from upstream's own installer,
# https://agentsview.io/install.sh (REPO="kenn-io/agentsview", asset
# agentsview_<version>_<os>_<arch>.tar.gz, checksums in SHA256SUMS), read and
# never run. Digests copied from that release's SHA256SUMS the same day; the
# linux_amd64 archive unpacks to one file `agentsview` whose sha256 equals the
# v0.46.1 binary this machine's agentsview.service runs. Pinned here, and the
# downloaded SHA256SUMS must agree, so an asset and checksum file replaced on
# GitHub together are still refused.
SHA256 = {
    ("linux", "amd64"): "e7822006d7bafa3aef9a5e4f7d7caffca56051bb039b848222a73633ed4c6c9b",  # pragma: allowlist secret (a published checksum)
    ("linux", "arm64"): "b170c7a70fab2c5d6297515b0f972aa3b43e1f3a677e7cc6b450d64ca730fb02",  # pragma: allowlist secret (a published checksum)
    ("darwin", "amd64"): "ed83679fc6ddcee02f1f66490ba0902fd8710dbf2bd5bb45172737dacbd340ea",  # pragma: allowlist secret (a published checksum)
    ("darwin", "arm64"): "83cd9872acf5bf59e77c149504431e317eed74537169a27f48d732db6f941415",  # pragma: allowlist secret (a published checksum)
}
HOST, PORT = "127.0.0.1", 8080
URL = f"http://{HOST}:{PORT}"
UNIT = "agentsview.service"
FAMILY = "agentsview"
LAUNCHD_LABEL = "com.headingos.agentsview"
DASHBOARD = "proxy-usage-dashboard"
DASHBOARD_UNITS = (f"{DASHBOARD}.service", f"{DASHBOARD}.timer")


class AgentViewError(RuntimeError):
    """A plain sentence for the installer to show."""


# ============================================================
# Where things are, and what the context injects
# ============================================================

def binary_path(home: Path) -> Path:
    """The versioned AgentsView binary the unit runs (placeholder AGENTSVIEW_BIN of the service layer)."""
    return home / ".local" / "share" / "heading-os" / "agentsview" / f"v{VERSION}" / "agentsview"


def unit_path(home: Path, unit: str = UNIT) -> Path:
    return home / ".config" / "systemd" / "user" / unit


def _home(ctx) -> Path:
    return Path(getattr(ctx, "home", None) or Path.home())


def _env(ctx) -> dict[str, str]:
    """The installer's child environment, HOME from the context, an injected systemctl first on PATH."""
    env = ctx.env()
    env["HOME"] = str(_home(ctx))
    return runner_env(env, getattr(ctx, "systemctl", None))


def _claude(ctx) -> str | None:
    return getattr(ctx, "claude", None) or shutil.which("claude", path=_env(ctx).get("PATH"))


def _agentsview(ctx) -> Path:
    return Path(getattr(ctx, "agentsview", None) or binary_path(_home(ctx)))


def _http_status(url: str) -> int | None:
    """The status of a GET to our own loopback server, or None when nothing answers."""
    from scripts.utils.user_agent import user_agent  # stdlib only

    req = urllib.request.Request(url, headers={"User-Agent": user_agent()})  # noqa: S310 - http to a fixed loopback address
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 - http to a fixed loopback address
            return resp.status
    except (OSError, ValueError):
        return None


def _tail(proc: subprocess.CompletedProcess, n: int = 8) -> str:
    return "\n".join(((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()[-n:])


def _foreign(path: Path) -> bool:
    """A unit file that exists and whose FIRST line is not the service layer's marker: adopted, left as it is.

    Ownership is the first line only, so a hand-written unit that quotes the
    marker in a later comment is still someone else's (HOS-308 review round 1).
    The one predicate for every ownership decision in this module; it asks the
    service layer's own `written_by_service_layer` (HOS-317), so this module and
    the layer cannot disagree.
    """
    from scripts.utils.schedule import written_by_service_layer

    return path.is_file() and not written_by_service_layer(path)


TIMEOUT_S = 600  # one `claude plugin` command or one service-layer call


def _services(ctx, verb: str, family: str) -> subprocess.CompletedProcess:
    """`heading-services.py <verb> <family>` of the installed engine, with the injected runner on PATH."""
    return subprocess.run([str(ctx.python),
                           str(ctx.engine / "scripts" / "heading-services.py"), verb, family],
                          capture_output=True, text=True, errors="replace", env=_env(ctx),
                          cwd=str(ctx.engine), timeout=TIMEOUT_S)


def _require_runner(ctx) -> None:
    problem = runner_problem(getattr(ctx, "systemctl", None))
    if problem:
        raise AgentViewError(problem)


def _claude_run(ctx, *args: str, timeout: int = TIMEOUT_S) -> subprocess.CompletedProcess:
    claude = _claude(ctx)
    if not claude:
        raise AgentViewError("Claude Code (`claude`) is not on PATH, so no plugin can be installed")
    return subprocess.run([claude, *args], capture_output=True, text=True, errors="replace",
                          env=_env(ctx), cwd=str(_home(ctx)), timeout=timeout)


def _claude_json(claude: str | None, home: Path, env: dict[str, str], *args: str):
    """`claude <args> --json` parsed, or None when it cannot be asked or answers no JSON."""
    if not claude:
        return None
    try:
        proc = subprocess.run([claude, *args, "--json"], capture_output=True, text=True, errors="replace",
                              env=env, cwd=str(home), timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return None


def _marketplaces(ctx) -> dict[str, dict] | None:
    """Registered catalogues by name, from `claude plugin marketplace list --json`.

    Shape READ 2026-11-01 on Claude Code 2.1.312: a JSON array of objects with
    `name` and `source`, plus `repo` for "github" or `path` for "directory",
    and `installLocation`. The built-in Anthropic directory is not listed.
    """
    data = _claude_json(_claude(ctx), _home(ctx), _env(ctx), "plugin", "marketplace", "list")
    if not isinstance(data, list):
        return None
    return {m["name"]: m for m in data if isinstance(m, dict) and isinstance(m.get("name"), str)}


def _same_path(a: str | None, b: Path) -> bool:
    try:
        return bool(a) and Path(a).resolve() == b.resolve()
    except OSError:
        return False


# ============================================================
# Step agent-watch
# ============================================================

def catalogue_dir(engine: Path) -> Path:
    return engine / "scripts" / "claude-mods"


def agent_watch_state(claude: str | None, home: Path, engine: Path,
                      env: dict[str, str] | None = None) -> tuple[bool, str]:
    """(enabled and read from this engine, the reason) from `claude plugin list --json`.

    Shape READ 2026-11-01 on Claude Code 2.1.312: a JSON array of objects with
    `id` ("agent-watch@heading-os-mods"), `scope`, `enabled` (true or false),
    `installPath`, and for a plugin of a folder catalogue `readFromFolder`,
    the folder Claude Code loads it from. Asked from the home directory, so a
    project's settings do not decide the answer. doctor.py reads this too.
    """
    data = _claude_json(claude, home, env if env is not None else dict(os.environ, HOME=str(home)),
                        "plugin", "list")
    if data is None:
        return False, "`claude plugin list --json` did not answer"
    entry = next((p for p in data if isinstance(p, dict) and p.get("id") == AGENT_WATCH), None) \
        if isinstance(data, list) else None
    want = catalogue_dir(engine) / "agent-watch"
    if entry is None:
        return False, f"{AGENT_WATCH} is not installed"
    if entry.get("enabled") is not True:
        return False, f"{AGENT_WATCH} is installed but not enabled"
    if not _same_path(entry.get("readFromFolder"), want):
        return False, f"{AGENT_WATCH} is read from {entry.get('readFromFolder')}, not {want}"
    return True, f"enabled, read from {want}"


def check_agent_watch(ctx) -> bool:
    return agent_watch_state(_claude(ctx), _home(ctx), ctx.engine, _env(ctx))[0]


def run_agent_watch(ctx) -> None:
    folder = catalogue_dir(ctx.engine)
    known = _marketplaces(ctx)
    if known is None:
        raise AgentViewError("`claude plugin marketplace list --json` did not answer")
    mine = known.get(MARKETPLACE)
    if mine is not None and not _same_path(mine.get("path"), folder):
        # The engine moved (AGENTVIEW.md part 1): forget the old folder, register this one.
        print(f"  {MARKETPLACE} pointed at {mine.get('path')}; registering {folder} instead")
        try:
            proc = _claude_run(ctx, "plugin", "marketplace", "remove", MARKETPLACE)
        except subprocess.TimeoutExpired as exc:
            raise AgentViewError(f"removing the old {MARKETPLACE} catalogue timed out after {TIMEOUT_S} s") from exc
        if proc.returncode != 0:
            raise AgentViewError(f"could not remove the old {MARKETPLACE} catalogue: {_tail(proc)}")
        mine = None
    if mine is None:
        try:
            proc = _claude_run(ctx, "plugin", "marketplace", "add", str(folder))
        except subprocess.TimeoutExpired as exc:
            raise AgentViewError(f"registering the {MARKETPLACE} catalogue timed out after {TIMEOUT_S} s") from exc
        if proc.returncode != 0:
            raise AgentViewError(f"could not register the {MARKETPLACE} catalogue at {folder}: {_tail(proc)}")
    ok, why = agent_watch_state(_claude(ctx), _home(ctx), ctx.engine, _env(ctx))
    if ok:
        return
    try:
        if why.endswith("not enabled"):
            proc = _claude_run(ctx, "plugin", "enable", AGENT_WATCH)
        else:
            proc = _claude_run(ctx, "plugin", "install", "--scope", "user", AGENT_WATCH)
    except subprocess.TimeoutExpired as exc:
        raise AgentViewError(f"{AGENT_WATCH} timed out after {TIMEOUT_S} s") from exc
    if proc.returncode != 0:
        raise AgentViewError(f"{AGENT_WATCH} could not be installed: {_tail(proc)}")


# ============================================================
# Step plugins
# ============================================================

def wanted_plugins(engine: Path) -> list[str]:
    """Every key whose value is `true` in enabledPlugins of the engine's .claude/settings.json."""
    try:
        settings = json.loads((engine / ".claude" / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AgentViewError(f"{engine / '.claude' / 'settings.json'} is unreadable: {exc}") from exc
    return sorted(k for k, v in (settings.get("enabledPlugins") or {}).items() if v is True)


def _installed_plugins(home: Path) -> set[str] | None:
    """The keys of the installed-plugins file under `home`, read where harness-audit.py reads it.

    `installed_plugins_path()` reads only the environment and the real home,
    so the path it would use under `home` is set for the call and put back.
    """
    spec = importlib.util.spec_from_file_location("_agentview_harness_audit",
                                                  ENGINE_ROOT / "scripts" / "harness-audit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    key = "HEADING_OS_INSTALLED_PLUGINS"
    old = os.environ.get(key)
    os.environ[key] = str(home / ".claude" / "plugins" / "installed_plugins.json")
    try:
        path = mod.installed_plugins_path()
    finally:
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    plugins = data.get("plugins") if isinstance(data, dict) else None
    return set(plugins) if isinstance(plugins, dict) else None


def missing_plugins(ctx) -> list[str]:
    installed = _installed_plugins(_home(ctx)) or set()
    return [k for k in wanted_plugins(ctx.engine) if k not in installed]


def check_plugins(ctx) -> bool:
    try:
        return not missing_plugins(ctx)
    except AgentViewError:
        return False


def run_plugins(ctx) -> None:
    missing = missing_plugins(ctx)
    failed: list[str] = []
    known = _marketplaces(ctx)
    if known is None:
        raise AgentViewError("`claude plugin marketplace list --json` did not answer")
    for market in sorted({k.rpartition("@")[2] for k in missing}):
        if market in known:
            continue
        source = MARKETPLACE_SOURCES.get(market)
        why = "no known source for it"
        proc = None
        if source:
            try:
                proc = _claude_run(ctx, "plugin", "marketplace", "add", source)
                why = _tail(proc, 2)
            except subprocess.TimeoutExpired:
                why = f"timed out after {TIMEOUT_S} s"
        if proc is None or proc.returncode != 0:
            print(f"  the {market} catalogue could not be registered: {why}")
            failed += [k for k in missing if k.rpartition("@")[2] == market]
    for name in missing:
        if name in failed:
            continue
        try:
            proc = _claude_run(ctx, "plugin", "install", name)
        except subprocess.TimeoutExpired:
            print(f"  {name} did not install: timed out after {TIMEOUT_S} s")
            failed.append(name)
            continue
        if proc.returncode != 0:
            print(f"  {name} did not install: {_tail(proc, 2)}")
            failed.append(name)
    if failed:
        raise AgentViewError(f"these plugins did not install: {', '.join(failed)}")


# ============================================================
# Step agentsview
# ============================================================

def _platform_key() -> tuple[str, str]:
    system = {"Linux": "linux", "Darwin": "darwin"}.get(platform.system(), platform.system().lower())
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(
        platform.machine().lower(), platform.machine().lower())
    return system, arch


def binary_ok(binary: Path) -> bool:
    """The binary starts and names VERSION: present is not proof, a corrupt one is present too.

    `agentsview --version` READ 2026-11-01 on this machine's v0.46.1:
    "agentsview v0.46.1 (commit 6552316..., built 2026-10-23T02:19:55Z)".
    """
    if not (binary.is_file() and os.access(binary, os.X_OK)):
        return False
    try:
        out = subprocess.run([str(binary), "--version"], capture_output=True, text=True, errors="replace",
                             timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return out.startswith(f"agentsview v{VERSION} ") or out.strip() == f"agentsview v{VERSION}"


def install_binary(home: Path) -> None:
    """AgentsView VERSION into binary_path(home), both checksums agreeing before anything is unpacked."""
    from scripts.utils.install_index import _sha256, download  # resumable, verified download (HOS-305)

    key = _platform_key()
    digest = SHA256.get(key)
    if not digest:
        raise AgentViewError(f"AgentsView {VERSION} publishes no build for {platform.system()} {platform.machine()}")
    name = f"agentsview_{VERSION}_{key[0]}_{key[1]}.tar.gz"
    target = binary_path(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as tmp:
        sums = Path(tmp) / "SHA256SUMS"
        from scripts.utils.user_agent import user_agent  # stdlib only

        req = urllib.request.Request(RELEASE + "SHA256SUMS", headers={"User-Agent": user_agent()})  # noqa: S310 - fixed https URL
        with urllib.request.urlopen(req, timeout=60) as resp, sums.open("wb") as out:  # noqa: S310 - fixed https URL
            shutil.copyfileobj(resp, out)
        listed = {parts[1].lstrip("*"): parts[0] for parts in
                  (ln.split() for ln in sums.read_text(encoding="utf-8").splitlines()) if len(parts) == 2}
        if listed.get(name) != digest:
            raise AgentViewError(f"the SHA256SUMS of AgentsView {VERSION} does not list {name} with the "
                                 "checksum this installer pins; nothing was installed")
        archive = target.parent / name
        if not (archive.is_file() and _sha256(archive) == digest):
            print(f"  downloading AgentsView {VERSION} ({name}); an interrupted download resumes on the next run")
            download(RELEASE + name, archive, digest)
        unpacked = Path(tmp) / "x"
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(unpacked, filter="data")
        got = unpacked / "agentsview"
        if not got.is_file():
            raise AgentViewError(f"{name} holds no `agentsview` binary; nothing was installed")
        os.chmod(got, 0o700)
        os.replace(got, target)
        archive.unlink()  # unpacked and verified; the archive is only a download cache
    if platform.system() == "Darwin":  # UNTESTED on a Mac; upstream's installer signs ad hoc the same way
        subprocess.run(["codesign", "-s", "-", str(target)], capture_output=True)
    if not binary_ok(target):
        raise AgentViewError(f"AgentsView {VERSION} was unpacked but does not start or reports another version")


def _unit_active(ctx) -> bool:
    if platform.system() == "Darwin":  # UNTESTED on a Mac; running, never only known to launchd (HOS-341)
        from scripts.utils.schedule import launchd_status

        return launchd_status(LAUNCHD_LABEL, _env(ctx))["running"]
    proc = subprocess.run([str(getattr(ctx, "systemctl", None) or "systemctl"), "--user", "is-active", UNIT],
                          capture_output=True, text=True, env=_env(ctx))
    return proc.stdout.strip() == "active"


def serve_status_running(ctx) -> bool:
    """`agentsview serve status` of the versioned binary names our address.

    Contract READ 2026-11-01 in upstream's source for v0.46.1
    (cmd/agentsview/serve_lifecycle.go, runServeStatus and serveStatusLines):
    it always exits 0, reads the daemon record under the data directory and
    starts nothing; a writable running server prints first the line
    "agentsview running at http://127.0.0.1:8080" followed by pid, version and
    uptime lines; otherwise a line such as "No agentsview server is running.".
    The same output was read from this machine's v0.46.1 the same day.
    """
    try:
        proc = subprocess.run([str(_agentsview(ctx)), "serve", "status"], capture_output=True, text=True,
                              errors="replace", env=_env(ctx), cwd=str(_home(ctx)), timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    lines = proc.stdout.splitlines()
    return bool(lines) and lines[0].strip() == f"agentsview running at {URL}" and "read-only" not in proc.stdout


def server_answers(get=None) -> bool:
    """A GET to http://127.0.0.1:8080 answers 200. doctor.py reads this too."""
    return (get or _http_status)(URL + "/") == 200


def unit_adopted(home: Path) -> bool:
    """An agentsview.service under `home` the service layer did not write (first line, `_foreign`)."""
    return _foreign(unit_path(home))


def check_agentsview(ctx) -> bool:
    try:
        return (binary_ok(_agentsview(ctx)) and _unit_active(ctx) and serve_status_running(ctx)
                and server_answers(getattr(ctx, "http_get", None)))
    except (OSError, subprocess.SubprocessError):
        return False


def _working(ctx, seconds: int = 30) -> bool:
    for _ in range(seconds):  # the server needs a moment after start
        if _unit_active(ctx) and serve_status_running(ctx) and server_answers(getattr(ctx, "http_get", None)):
            return True
        time.sleep(1)
    return False


def run_agentsview(ctx) -> None:
    home = _home(ctx)
    if not binary_ok(_agentsview(ctx)):
        install_binary(home)
    if platform.system() != "Darwin" and unit_adopted(home):
        print(f"  adopted: {unit_path(home)} was not written by HEADING OS and is left as it is")
        if not _working(ctx, 10):
            raise AgentViewError(f"the existing {UNIT} is not running or does not answer at {URL}; "
                                 f"restart it with: systemctl --user restart {UNIT}")
        return
    _require_runner(ctx)
    try:
        proc = _services(ctx, "install", FAMILY)
    except subprocess.TimeoutExpired as exc:
        raise AgentViewError(f"the service layer did not finish installing {FAMILY} in {TIMEOUT_S} s") from exc
    if proc.returncode != 0:
        raise AgentViewError(f"the service layer could not install {FAMILY}: {_tail(proc)}")
    if not _working(ctx):
        raise AgentViewError(f"{UNIT} is installed but AgentsView does not answer at {URL}")


# ============================================================
# Step usage-dashboard
# ============================================================

def check_usage_dashboard(ctx) -> bool:
    """`heading-services.py status proxy-usage-dashboard` exits 0: unit present, enabled and active."""
    try:
        return Path(ctx.python).exists() and _services(ctx, "status", DASHBOARD).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def run_usage_dashboard(ctx) -> None:
    home = _home(ctx)
    foreign = [unit_path(home, u) for u in DASHBOARD_UNITS] if platform.system() != "Darwin" else []
    foreign = [p for p in foreign if _foreign(p)]
    if foreign:
        print(f"  adopted: {', '.join(map(str, foreign))} not written by HEADING OS, left as they are")
        if not check_usage_dashboard(ctx):
            raise AgentViewError(f"the existing {DASHBOARD} units are not enabled and active; turn them on with: "
                                 f"systemctl --user enable --now {DASHBOARD}.timer")
        return
    _require_runner(ctx)
    try:
        proc = _services(ctx, "install", DASHBOARD)
    except subprocess.TimeoutExpired as exc:
        raise AgentViewError(f"the service layer did not finish installing {DASHBOARD} in {TIMEOUT_S} s") from exc
    if proc.returncode != 0:
        raise AgentViewError(f"the service layer could not install {DASHBOARD}: {_tail(proc)}")
