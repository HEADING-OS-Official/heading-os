#!/usr/bin/env python3
"""Hindsight, the memory server, installed and wired by `scripts/heading-install.py` (HOS-304).

Usage (from the installer, never by hand):
    from scripts.utils import install_hindsight as hs
    hs.check_server_install(ctx); hs.run_server_install(ctx)   # and settings, service, bank, claude, timers

Slice 4 of the approved deployment design (registry name `plans`,
2026-11-01-heading-os-ideal-deployment-design, sections 2, 6 and 7; audit A4:
today five hand edits). Six steps, each a check that reads the real state and a
run that repairs only what the check found missing. What they leave, relative
to the executive's home:

    ~/.local/share/heading-os/hindsight/<version>/   hindsight-api in its own uv environment
    ~/.local/bin/hindsight                           the Hindsight command line, checksum-verified
    ~/.config/heading-os/hindsight/server.env        the server's settings and key (mode 600, folder 700)
    ~/.config/hindsight/secrets/hindsight-api-key    the same key, where the engine looks (HINDSIGHT.md 5.1)
    ~/.config/hindsight/mcp-headers.json             the same key, for the MCP bridge (5.3)
    ~/.hindsight/config                              the same key, for the command line (4.1)
    ~/.hindsight/coding-agent.json                   the plugin's settings, the key as apiToken (5.4)
    ~/.config/heading-os/hindsight/console.env       the same key and the web console's own login key (mode 600, HOS-312)
    hindsight-server.service                         the service layer's family `hindsight-server`
    bank `main`, the MCP server `hindsight-main`, the coding-agents plugin,
    and the families hindsight-sync, hindsight-snapshot

The server is the guide's pip route (HINDSIGHT.md 3.3), no Docker: `hindsight-api`
brings its own embedded PostgreSQL (`pg0-embedded`, MEASURED 2026-11-01 by
`uv pip compile` of hindsight-api==1.1.0 for Linux x86-64, Python 3.11: 220
packages, among them pg0-embedded 0.15.2, torch and sentence-transformers, so
the first install is a large download). It answers on 127.0.0.1 only.

The extraction model is Claude Sonnet through the local CLIProxyAPI that the
HOS-307 steps set up, signed in with the executive's own Claude subscription
(design section 6 item 4). UNVERIFIED that Hindsight runs well that way: HELM
tests it on a VM in HOS-312.

Every function takes its home, its `systemctl`, `uv`, `hindsight`, `claude` and
`npx`, a subprocess runner and an HTTP opener from the step context
(`getattr(ctx, name, None)`, else the real one), so a proof run points all of
them at scratch and stubs. An injected `systemctl` leads the PATH of every
child (scripts/utils/install_runner.py). No secret is printed, logged or passed
on a command line: the key goes from `secrets` into files inside this process,
and the command line's own `configure` is not used because it takes the key
only on argv (`hindsight configure --help`, 1.1.0, read 2026-11-01).

Standard library only at import time, like the installer. Linux (systemd user
units) is the path; on macOS the service step refuses (MACOS_UNSUPPORTED) and
the service layer offers the family on Linux only.
"""

from __future__ import annotations

import glob
import hashlib
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
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.utils.install_herdr_cliproxy import PROXY_BASE_URL, PROXY_KEY_ENV  # noqa: E402
from scripts.utils.install_commands import bare_home_tools, claude_settings_path, hook_commands, home_tools  # noqa: E402 - HOS-312
from scripts.utils.install_runner import changed_since_start, runner_env, runner_problem, service_pid  # noqa: E402
from scripts.utils.paths import iter_env_pairs  # noqa: E402

# The version of this machine's Hindsight command line (`hindsight --version`,
# read 2026-11-01) and the one HINDSIGHT.md was checked against. PyPI has
# hindsight-api 1.1.0 (MEASURED 2026-11-01), so no newer 1.1.x was needed.
VERSION = "1.1.0"
PACKAGE = "hindsight-api"
# The coding-agents plugin as installed on the operator's machine, read from
# ~/.hindsight/coding-agents/package.json on 2026-11-01 (read only): 0.8.2, the
# version the guide was checked with.
PLUGIN_VERSION = "0.8.2"
PLUGIN_PACKAGE = "@vectorize-io/hindsight-coding-agents"
API_URL = "http://127.0.0.1:8888"
BANK = "main"
BANK_NAME = "HEADING OS"
MCP_NAME = "hindsight-main"
UNIT = "hindsight-server.service"
LAUNCHD_LABEL = "com.headingos.hindsight-server"  # schedule.LAUNCHD_PREFIX + the service stem
TIMER_FAMILIES = ("hindsight-sync", "hindsight-snapshot")
READY_WAIT_S = 900  # the first start fetches the embedding models and creates the database
# The command line, from the v1.1.0 GitHub release of github.com/vectorize-io/hindsight.
# Each SHA-256 is the asset `digest` of the GitHub release API (MEASURED 2026-11-01);
# the linux-amd64 one equals the sha256sum of the operator's own ~/.local/bin/hindsight,
# which `hindsight --version` reports as 1.1.0. Pinned here, so an asset replaced
# later on GitHub is refused.
CLI_URL = f"https://github.com/vectorize-io/hindsight/releases/download/v{VERSION}/hindsight-{{target}}"
CLI_SHA256 = {
    "linux-amd64": "6ab6f55912b336a029a0f0dd8de4d76fe6ca0ac131b1b49061d1191ec93a662a",  # pragma: allowlist secret (a public release checksum)
    "linux-arm64": "5bea4704fb027826c809754f86a1c25562b009db5423f51d419a53e03c635471",  # pragma: allowlist secret (a public release checksum)
    "darwin-amd64": "35327ec87baae379e76857e6825d6084878af9738e2e5c74663ed20a061e8772",  # pragma: allowlist secret (a public release checksum)
    "darwin-arm64": "13ca91dd8142daeb66c4be3c0486b6f0d20e530b8909b7328879c452a2d5565d",  # pragma: allowlist secret (a public release checksum)
}
# The server's settings. The tenant pair is HINDSIGHT.md 3.1. The extraction
# names were read from the installed package's own settings source, hindsight_api
# 1.1.0 `config.py` (ENV_LLM_PROVIDER, ENV_LLM_API_KEY, ENV_LLM_MODEL,
# ENV_LLM_BASE_URL), and `engine/llm_wrapper.py` sends provider `openai` to its
# OpenAI-compatible client with that base URL (MEASURED 2026-11-01 from the PyPI
# wheel, sha256 e9bc7964... for hindsight_api, d19a323a... for hindsight_api_slim).
TENANT_EXTENSION = "hindsight_api.extensions.builtin.tenant:ApiKeyTenantExtension"
ENV_TENANT_EXTENSION = "HINDSIGHT_API_TENANT_EXTENSION"
ENV_TENANT_KEY = "HINDSIGHT_API_TENANT_API_KEY"
# The web console sends the key from this variable (HOS-312: bin/cli.js and the
# server chunks of @vectorize-io/hindsight-control-plane 1.1.0, read 2026-11-02).
ENV_CONSOLE_KEY = "HINDSIGHT_CP_DATAPLANE_API_KEY"  # pragma: allowlist secret (a setting name, no value)
# The console's login key: set, its middleware asks for the login cookie (HOS-312 round 2).
ENV_CONSOLE_ACCESS = "HINDSIGHT_CP_ACCESS_KEY"  # pragma: allowlist secret (a setting name, no value)
ENV_LLM_PROVIDER = "HINDSIGHT_API_LLM_PROVIDER"
ENV_LLM_BASE_URL = "HINDSIGHT_API_LLM_BASE_URL"
ENV_LLM_API_KEY = "HINDSIGHT_API_LLM_API_KEY"  # pragma: allowlist secret (a setting name, no value)
ENV_LLM_MODEL = "HINDSIGHT_API_LLM_MODEL"
LLM_PROVIDER = "openai"
ENGINE_ROOT = Path(__file__).resolve().parent.parent.parent
# HELM's decision, HOS-304 review round 1: the family is Linux only, because the
# launchd translation does not carry the unit's EnvironmentFile, so a Mac server
# would start without its key and its extraction settings.
MACOS_UNSUPPORTED = ("the local Hindsight memory server is not yet supported on macOS; "
                     "HEADING OS installs it on Linux and WSL2 only for now")


class HindsightError(OSError):
    """A plain sentence for the user, never a secret. An OSError, so the installer's
    step loop reports it as a failed step without a handler of its own."""


# ============================================================
# Where things are
# ============================================================

def env_path(home: Path) -> Path:
    """The uv environment of the server, named after the installed version."""
    return home / ".local" / "share" / "heading-os" / "hindsight" / VERSION


def server_env_path(home: Path) -> Path:
    return home / ".config" / "heading-os" / "hindsight" / "server.env"


def console_env_path(home: Path) -> Path:
    """The web console's one setting, the same key (scripts/utils/install_hindsight_console.py, HOS-312)."""
    return home / ".config" / "heading-os" / "hindsight" / "console.env"


def key_file(home: Path) -> Path:
    return home / ".config" / "hindsight" / "secrets" / "hindsight-api-key"


def mcp_headers_path(home: Path) -> Path:
    return home / ".config" / "hindsight" / "mcp-headers.json"


def cli_config_path(home: Path) -> Path:
    return home / ".hindsight" / "config"


def coding_agent_path(home: Path) -> Path:
    return home / ".hindsight" / "coding-agent.json"


def plugin_package_path(home: Path) -> Path:
    return home / ".hindsight" / "coding-agents" / "package.json"


def cli_path(home: Path) -> Path:
    return home / ".local" / "bin" / "hindsight"


def unit_path(ctx) -> Path:
    """Where the service layer, run with this context's environment, writes the unit."""
    from scripts.utils.schedule import systemd_user_dir

    return systemd_user_dir(_env(ctx)) / UNIT


# ============================================================
# The context a step acts on
# ============================================================

def _home(ctx) -> Path:
    return Path(getattr(ctx, "home", None) or Path.home())


def _injected(ctx) -> str | None:
    return getattr(ctx, "systemctl", None)


def _systemctl(ctx) -> str:
    return str(_injected(ctx) or "systemctl")


def _env(ctx) -> dict[str, str]:
    """The installer's child environment: HOME at this context's home, the injected
    service manager first on PATH, and no inherited Hindsight address or key, so
    the command line reads the config this install wrote."""
    env = ctx.env()
    env["HOME"] = str(_home(ctx))
    for name in ("HINDSIGHT_API_URL", "HINDSIGHT_API_KEY", "HINDSIGHT_PROFILE"):
        env.pop(name, None)
    return runner_env(env, _injected(ctx))


def _tool(ctx, name: str) -> str | None:
    found = getattr(ctx, name, None)
    return str(found) if found else shutil.which(name, path=_env(ctx).get("PATH", ""))


def _hindsight(ctx) -> str:
    return str(getattr(ctx, "hindsight", None) or cli_path(_home(ctx)))


def _run(ctx, cmd: list[str], *, stdin: str | None = None, timeout: int | None = 600,
         cwd: Path | None = None, capture: bool = True,
         extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Through the context's runner; never raises for a program that cannot start (exit 127)."""
    runner = getattr(ctx, "runner", None) or subprocess.run
    try:
        return runner(cmd, input=stdin, capture_output=capture, text=True, errors="replace",
                      env={**_env(ctx), **(extra_env or {})}, cwd=str(cwd or _home(ctx)), timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(cmd, 127, stdout="", stderr=str(exc))


def _why(proc: subprocess.CompletedProcess) -> str:
    """The last stderr line only: stdout of these commands can hold a key."""
    lines = (proc.stderr or "").strip().splitlines()
    return lines[-1][-300:] if lines else f"exit {proc.returncode}"


def _open(ctx, url: str, headers: dict[str, str] | None = None, timeout: int = 10):
    opener = getattr(ctx, "opener", None) or urllib.request.urlopen
    from scripts.utils.user_agent import user_agent

    req = urllib.request.Request(url, headers={"User-Agent": user_agent(), **(headers or {})})  # noqa: S310 - fixed loopback or https release URL
    return opener(req, timeout=timeout)


def _write_private(path: Path, text: str, mode: int = 0o600, dir_mode: int = 0o700) -> None:
    """Atomic, and private before its first byte (mkstemp creates mode 600)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, dir_mode)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _private(path: Path) -> bool:
    """A file holding the key: mode 600 or stricter, in a folder no group or other user may write.

    A check that accepts the file without this would leave a key another local
    user can read, because the run that repairs the mode would never start
    (HOS-304 review round 1).
    """
    try:
        return (path.is_file() and not path.stat().st_mode & 0o077
                and not path.parent.stat().st_mode & 0o022)
    except OSError:
        return False


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _version(text: str) -> tuple[int, ...] | None:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(x) for x in m.groups()) if m else None


def _pinned(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


# ============================================================
# Step hindsight-server: the package and the command line
# ============================================================

def installed_version(home: Path) -> str | None:
    """`hindsight-api`'s version from its package metadata in our environment (the
    server has no --version flag: hindsight_api 1.1.0 main.py, read 2026-11-01)."""
    env = env_path(home)
    if not os.access(env / "bin" / "hindsight-api", os.X_OK):
        return None
    for meta in glob.glob(str(env / "lib" / "python*" / "site-packages" / "hindsight_api-*.dist-info" / "METADATA")):
        m = re.search(r"^Version:\s*(\S+)\s*$", _read(Path(meta)) or "", re.M)
        if m:
            return m.group(1)
    return None


def cli_version(ctx) -> tuple[int, ...] | None:
    proc = _run(ctx, [_hindsight(ctx), "--version"], timeout=30)
    return _version(proc.stdout) if proc.returncode == 0 and proc.stdout.startswith("hindsight ") else None


def check_server_install(ctx) -> bool:
    cli = cli_version(ctx)
    return installed_version(_home(ctx)) == VERSION and cli is not None and cli >= _pinned(VERSION)


def _cli_target() -> str:
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    osname = {"Linux": "linux", "Darwin": "darwin"}.get(platform.system())
    target = f"{osname}-{arch}"
    if target not in CLI_SHA256:
        raise HindsightError(f"Hindsight {VERSION} publishes no command line for {platform.system()} {machine}")
    return target


def _install_cli(ctx) -> None:
    target = cli_path(_home(ctx))
    name = _cli_target()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as tmp:
        dl = Path(tmp) / "hindsight"
        try:
            with _open(ctx, CLI_URL.format(target=name), timeout=300) as resp, dl.open("wb") as out:
                shutil.copyfileobj(resp, out)
        except (urllib.error.URLError, OSError) as exc:
            raise HindsightError(f"the Hindsight command line could not be downloaded: {exc}") from exc
        if hashlib.sha256(dl.read_bytes()).hexdigest() != CLI_SHA256[name]:
            raise HindsightError(f"hindsight-{name} does not match the checksum Hindsight {VERSION} "
                                 "published; nothing was installed")
        os.chmod(dl, 0o700)
        os.replace(dl, target)


def run_server_install(ctx) -> None:
    home = _home(ctx)
    if installed_version(home) != VERSION:
        uv = _tool(ctx, "uv")
        if not uv:
            raise HindsightError("uv is not on PATH; the prerequisites step installs it")
        env = env_path(home)
        env.parent.mkdir(parents=True, exist_ok=True)
        print(f"  {PACKAGE} {VERSION} and its libraries: a large download (several GB), once")
        proc = _run(ctx, [uv, "venv", "--quiet", "--allow-existing", "--python", "3.11", str(env)])
        if proc.returncode != 0:
            raise HindsightError(f"the Hindsight environment could not be created: {_why(proc)}")
        proc = _run(ctx, [uv, "pip", "install", "--quiet", "--python", str(env / "bin" / "python"),
                          f"{PACKAGE}=={VERSION}"], timeout=None)
        if proc.returncode != 0:
            raise HindsightError(f"{PACKAGE} {VERSION} could not be installed: {_why(proc)}")
    cli = cli_version(ctx)
    if cli is None or cli < _pinned(VERSION):
        if getattr(ctx, "hindsight", None):
            raise HindsightError(f"the Hindsight command line at {_hindsight(ctx)} is not {VERSION} or newer")
        _install_cli(ctx)


# ============================================================
# Step hindsight-settings: the key and the server's settings
# ============================================================

def read_server_env(home: Path) -> dict[str, str]:
    return dict(iter_env_pairs(_read(server_env_path(home)) or ""))


def _cli_config_values(text: str) -> dict[str, str]:
    """`key = "value"` lines of ~/.hindsight/config. The format is TOML with the two
    keys api_url and api_key, double-quoted (MEASURED 2026-11-01 on this machine's
    own file, key names and quoting only, written by `hindsight configure` 1.1.0)."""
    out = {}
    for line in text.splitlines():
        m = re.fullmatch(r'\s*([a-z_]+)\s*=\s*"([^"\\]*)"\s*', line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _header_key(text: str | None) -> str | None:
    try:
        auth = json.loads(text or "").get("Authorization", "")
    except (ValueError, AttributeError):
        return None
    m = re.fullmatch(r"Bearer (\S+)", auth) if isinstance(auth, str) else None
    return m.group(1) if m else None


def key_places(home: Path) -> dict[str, str | None]:
    """The key as each place step 2 owns holds it; None where a place holds none."""
    return {
        str(key_file(home)): (_read(key_file(home)) or "").strip() or None,
        str(mcp_headers_path(home)): _header_key(_read(mcp_headers_path(home))),
        str(cli_config_path(home)): _cli_config_values(_read(cli_config_path(home)) or "").get("api_key") or None,
        str(server_env_path(home)): read_server_env(home).get(ENV_TENANT_KEY) or None,
    }


def the_key(home: Path) -> str | None:
    """The one key every place holds, or None when any place is missing it or they differ."""
    values = set(key_places(home).values())
    return values.pop() if len(values) == 1 and None not in values else None


def proxy_key(ctx) -> str:
    for name, value in iter_env_pairs(_read(ctx.engine / ".env") or ""):
        if name == PROXY_KEY_ENV:
            return value
    return ""


def _settings_ok(ctx, settings: dict[str, str]) -> bool:
    key = proxy_key(ctx)
    return (settings.get(ENV_TENANT_EXTENSION) == TENANT_EXTENSION and bool(settings.get(ENV_TENANT_KEY))
            and settings.get(ENV_LLM_PROVIDER) == LLM_PROVIDER and settings.get(ENV_LLM_BASE_URL) == PROXY_BASE_URL
            and bool(key) and settings.get(ENV_LLM_API_KEY) == key
            and settings.get(ENV_LLM_MODEL, "").startswith("claude-sonnet"))


def check_settings(ctx) -> bool:
    """No network: the files, their modes, and one key in every place."""
    home = _home(ctx)
    return (_private(server_env_path(home)) and _private(key_file(home)) and _private(mcp_headers_path(home))
            and _private(cli_config_path(home)) and the_key(home) is not None
            and _settings_ok(ctx, read_server_env(home))
            and _cli_config_values(_read(cli_config_path(home)) or "").get("api_url") == API_URL
            and _private(console_env_path(home)) and _console_ok(home))


def _console_ok(home: Path) -> bool:
    """console.env holds the data-plane key and a login key of its own, never the same one."""
    console = dict(iter_env_pairs(_read(console_env_path(home)) or ""))
    access = console.get(ENV_CONSOLE_ACCESS) or ""
    return console.get(ENV_CONSOLE_KEY) == the_key(home) and bool(access) and access != the_key(home)


def _latest_sonnet(ctx) -> str:
    """`claude_models.latest("sonnet")` of the installed engine, at install time."""
    proc = _run(ctx, [str(ctx.python), "-c", "from scripts.utils.claude_models import latest; print(latest('sonnet'))"],
                cwd=ctx.engine, timeout=60)
    model = proc.stdout.strip().splitlines()[-1] if proc.returncode == 0 and proc.stdout.strip() else ""
    if not re.fullmatch(r"claude-sonnet-[A-Za-z0-9.-]+", model):
        raise HindsightError(f"the engine names no Claude Sonnet model: {_why(proc)}")
    return model


def _with_values(text: str, values: dict[str, str], line_of) -> str:
    """`text` with each of `values` set in place, or appended; every other line kept."""
    lines, done = text.splitlines(), set()
    for i, line in enumerate(lines):
        for name, value in values.items():
            if re.match(rf"\s*{re.escape(name)}\s*=", line):
                lines[i] = line_of(name, value)
                done.add(name)
    lines += [line_of(n, v) for n, v in values.items() if n not in done]
    return "\n".join(lines) + "\n"


def run_settings(ctx) -> None:
    home = _home(ctx)
    found = {place: value for place, value in key_places(home).items() if value}
    if len(set(found.values())) > 1:
        raise HindsightError("these files hold different Hindsight keys: " + ", ".join(sorted(found))
                             + ". The installer does not choose one; decide which key your server uses, "
                             "make every file hold it (or remove the wrong files), then run this again")
    key = next(iter(found.values()), None) or secrets.token_hex(32)
    pkey = proxy_key(ctx)
    if not pkey:
        raise HindsightError(f"{PROXY_KEY_ENV} is not in {ctx.engine / '.env'}; the CLIProxyAPI steps come first")
    current = read_server_env(home)
    model = current.get(ENV_LLM_MODEL, "")
    if not model.startswith("claude-sonnet"):
        model = _latest_sonnet(ctx)
    settings = {ENV_TENANT_EXTENSION: TENANT_EXTENSION, ENV_TENANT_KEY: key, ENV_LLM_PROVIDER: LLM_PROVIDER,
                ENV_LLM_BASE_URL: PROXY_BASE_URL, ENV_LLM_API_KEY: pkey, ENV_LLM_MODEL: model}
    old = _read(server_env_path(home)) or ("# Hindsight server settings, written by scripts/heading-install.py "
                                           "(HOS-304). Private: it holds two keys.\n")
    _write_private(server_env_path(home), _with_values(old, settings, lambda n, v: f"{n}={v}"))
    _write_private(key_file(home), key + "\n")
    _write_private(mcp_headers_path(home), json.dumps({"Authorization": f"Bearer {key}"}) + "\n")
    _write_private(cli_config_path(home),
                   _with_values(_read(cli_config_path(home)) or "", {"api_url": API_URL, "api_key": key},
                                lambda n, v: f'{n} = "{v}"'),
                   dir_mode=0o700)
    old = _read(console_env_path(home)) or ("# Hindsight web console settings, written by scripts/heading-install.py "
                                            "(HOS-312). Private: it holds two keys.\n")
    # The login key survives a rerun, so the executive is never logged out by one.
    access = dict(iter_env_pairs(old)).get(ENV_CONSOLE_ACCESS) or secrets.token_urlsafe(32)
    _write_private(console_env_path(home), _with_values(old, {ENV_CONSOLE_KEY: key, ENV_CONSOLE_ACCESS: access},
                                                        lambda n, v: f"{n}={v}"))


# ============================================================
# Step hindsight-service: the server in the background
# ============================================================

def tunnel_conflict(ctx) -> str | None:
    """The service layer's own reason to leave hindsight-server off here, or None,
    asked with this context's environment, as its child heading-services.py asks."""
    from scripts.utils.schedule import conflict_reason, select_families

    return conflict_reason(select_families(["hindsight-server"])[0], _env(ctx))


def server_ready(ctx, key: str | None = None) -> bool:
    """A real request: /health/ready (the path hindsight-health.py uses) answers 200."""
    key = key or the_key(_home(ctx))
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        with _open(ctx, f"{API_URL}/health/ready", headers) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _services(ctx, verb: str, *names: str, capture: bool = True) -> subprocess.CompletedProcess:
    """`scripts/heading-services.py <verb> <names>`, on the injected runner when there is one."""
    problem = runner_problem(_injected(ctx))
    if problem:
        raise HindsightError(problem)
    return _run(ctx, [str(ctx.python), str(ctx.engine / "scripts" / "heading-services.py"), verb, *names],
                cwd=ctx.engine, capture=capture, timeout=None)


def _server_pid(ctx) -> int | None:
    return service_pid(_systemctl(ctx), UNIT, LAUNCHD_LABEL, _env(ctx))


def _adopted(ctx) -> bool:
    """A hindsight-server.service the service layer did not write: someone else's, never restarted here."""
    from scripts.utils.schedule import written_by_service_layer

    path = unit_path(ctx)
    return path.exists() and not written_by_service_layer(path)


def check_service(ctx) -> bool:
    if getattr(ctx, "dry_run", False) or platform.system() == "Darwin":
        return False  # its check asks the server, and a dry run sends nothing
    home = _home(ctx)
    if tunnel_conflict(ctx) or not ctx.python.exists():
        return False
    return (_services(ctx, "status", "hindsight-server").returncode == 0 and server_ready(ctx)
            and not changed_since_start(server_env_path(home), _server_pid(ctx)))


def _restart(ctx) -> None:
    """Settings are read when the server starts; an adopted unit is never restarted here."""
    if _adopted(ctx):
        print(f"  {unit_path(ctx)} was not written by HEADING OS and is left untouched, so it is not "
              f"restarted. To load the new settings, run yourself: systemctl --user restart {UNIT}")
        return
    else:
        cmd = [_systemctl(ctx), "--user", "restart", UNIT]
    proc = _run(ctx, cmd, timeout=120)
    if proc.returncode != 0:
        raise HindsightError(f"the Hindsight server could not be restarted to load its settings: {_why(proc)}")


def run_service(ctx) -> None:
    if platform.system() == "Darwin":
        raise HindsightError(MACOS_UNSUPPORTED)
    home = _home(ctx)
    conflict = tunnel_conflict(ctx)
    if conflict:
        raise HindsightError(conflict)
    if _services(ctx, "install", "hindsight-server", capture=False).returncode != 0:
        raise HindsightError("the service layer could not install hindsight-server; see the lines above "
                             "(scripts/heading-services.py install hindsight-server)")
    if changed_since_start(server_env_path(home), _server_pid(ctx)):
        _restart(ctx)
    deadline = time.monotonic() + READY_WAIT_S
    while not server_ready(ctx):
        if time.monotonic() >= deadline:
            raise HindsightError(f"hindsight-server runs but {API_URL}/health/ready does not answer 200; "
                                 f"read its log: journalctl --user -u {UNIT} -n 50")
        time.sleep(2)


# ============================================================
# Step hindsight-bank: the bank main
# ============================================================

def bank_listed(output: str, bank: str = BANK) -> bool:
    """Whether `hindsight bank list -o json` names `bank`.

    The server answers GET /v1/default/banks with {"banks": [{"bank_id": ...}]}
    (BankListResponse in hindsight_api 1.1.0 api/http.py, read 2026-11-01). That
    the command line's `-o json` passes those objects through is # ASSUMED, so
    any object in the output whose bank_id is `bank` counts.
    """
    try:
        data = json.loads(output)
    except ValueError:
        return False
    stack = [data]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if item.get("bank_id") == bank:
                return True
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False


def _bank_list(ctx) -> subprocess.CompletedProcess:
    return _run(ctx, [_hindsight(ctx), "bank", "list", "-o", "json"], timeout=60)


def check_bank(ctx) -> bool:
    if getattr(ctx, "dry_run", False):
        return False
    proc = _bank_list(ctx)
    return proc.returncode == 0 and bank_listed(proc.stdout)


def run_bank(ctx) -> None:
    listed = _bank_list(ctx)
    if listed.returncode != 0:
        raise HindsightError(f"`hindsight bank list` failed: {_why(listed)}")
    if bank_listed(listed.stdout):
        return
    proc = _run(ctx, [_hindsight(ctx), "bank", "create", BANK, "--name", BANK_NAME], timeout=60)
    if proc.returncode != 0:
        raise HindsightError(f"the bank {BANK} could not be created: {_why(proc)}")


# ============================================================
# Step hindsight-claude: the MCP bridge, the plugin, its settings
# ============================================================

def bridge_command(ctx) -> tuple[str, list[str]]:
    """HINDSIGHT.md 5.3 step 3, full paths into the installed engine."""
    return str(ctx.engine / ".venv" / "bin" / "python"), [str(ctx.engine / "scripts" / "hindsight-mcp-bridge.py"), "serve"]


def registered_bridge(home: Path) -> tuple[str | None, list | None]:
    from scripts.utils.install_lific import claude_config_path

    try:
        data = json.loads(claude_config_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    entry = (data.get("mcpServers") or {}).get(MCP_NAME) if isinstance(data, dict) else None
    return (entry.get("command"), entry.get("args")) if isinstance(entry, dict) else (None, None)


def plugin_version(home: Path) -> tuple[int, ...] | None:
    try:
        return _version(json.loads(_read(plugin_package_path(home)) or "").get("version", ""))
    except (ValueError, AttributeError):
        return None


def coding_agent_values(ctx, key: str) -> dict:
    """The keys HINDSIGHT.md 5.4 step 2 lists, for this install's engine and data."""
    paths = [str(ctx.engine), str(ctx.data)]
    return {"apiUrl": API_URL, "apiToken": key, "serverMode": "self-hosted", "optInOnly": True,
            "optInPaths": paths, "mapPathToBank": dict.fromkeys(paths, BANK), "manageBankConfig": False,
            "gitIngest": "none"}


def _coding_agent(home: Path) -> dict | None:
    try:
        data = json.loads(_read(coding_agent_path(home)) or "")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def check_claude(ctx) -> bool:
    home = _home(ctx)
    command, args = bridge_command(ctx)
    key = the_key(home)
    current = _coding_agent(home) or {}
    version = plugin_version(home)
    return (registered_bridge(home) == (command, args) and key is not None
            and _private(coding_agent_path(home)) and version is not None and version >= _pinned(PLUGIN_VERSION)
            and all(current.get(k) == v for k, v in coding_agent_values(ctx, key).items())
            and not bare_home_tools(home, home_tools(home, ctx.engine)))


def absolute_hooks(ctx) -> None:
    """Every Claude Code hook command that starts with a bare home tool, rewritten to its absolute path.

    Only the leading name changes, to the shell-quoted path from
    install_commands.home_tools; every other byte of the command and of the
    file stays. Written through a .tmp file and os.replace, keeping the mode
    (HOS-312, F5: a session started before the install has no Node.js on PATH).
    """
    home = _home(ctx)
    tools = home_tools(home, ctx.engine)
    path = claude_settings_path(home)
    try:
        text = path.read_text(encoding="utf-8")
        settings = json.loads(text)
    except (OSError, ValueError) as exc:
        # No settings file, or not JSON: nothing is registered there to rewrite.
        print(f"  note: {path} not read ({type(exc).__name__}); no hook command rewritten")
        return
    replaced: list[tuple[str, str]] = []
    for _event, hook in hook_commands(settings):
        command = hook["command"]
        if (command == "node" or command.startswith("node ")) and "node" not in tools:
            raise HindsightError("a Claude Code hook calls node, but Node.js is not where the prerequisites "
                                 "step installs it; run the installer again from the start")
        for name, absolute in tools.items():
            if command == name or command.startswith(name + " "):
                hook["command"] = shlex.quote(absolute) + command[len(name):]
                replaced.append((command, hook["command"]))
                break
    if not replaced:
        return
    # Each command string replaced where it stands, so the rest of the file keeps its bytes;
    # a file whose text does not parse back to the rewritten settings is written whole instead.
    new_text = text
    for before, after in replaced:
        for encoded in (json.dumps(before, ensure_ascii=False), json.dumps(before)):
            if encoded in new_text:
                new_text = new_text.replace(encoded, json.dumps(after, ensure_ascii=False))
                break
    try:
        same = json.loads(new_text) == settings
    except ValueError:
        same = False
    if not same:
        new_text = json.dumps(settings, indent=2) + "\n"
    mode = path.stat().st_mode & 0o777
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(new_text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def run_claude(ctx) -> None:
    home = _home(ctx)
    key = the_key(home)
    if key is None:
        raise HindsightError("the Hindsight key is not in place; the settings step comes first")
    command, args = bridge_command(ctx)
    claude = _tool(ctx, "claude")
    if not claude:
        raise HindsightError("Claude Code (`claude`) is not on PATH, so Hindsight cannot be registered with it")
    if registered_bridge(home) != (command, args):
        if registered_bridge(home) != (None, None):
            # Another hindsight-main at user scope: replaced by the engine's bridge.
            _run(ctx, [claude, "mcp", "remove", "--scope", "user", MCP_NAME], timeout=60)
        proc = _run(ctx, [claude, "mcp", "add", "--scope", "user", "--transport", "stdio", MCP_NAME, "--",
                          command, *args], timeout=60)
        if proc.returncode != 0:
            raise HindsightError(f"`claude mcp add {MCP_NAME}` failed: {_why(proc)}")
    version = plugin_version(home)
    if version is None or version < _pinned(PLUGIN_VERSION):
        npx = _tool(ctx, "npx")
        if not npx:
            raise HindsightError("npx is not on PATH; the prerequisites step installs Node.js")
        proc = _run(ctx, [npx, "--yes", f"{PLUGIN_PACKAGE}@{PLUGIN_VERSION}", "install", "claude-code",
                          "--server", "self-hosted", "--api-url", API_URL], timeout=900)
        if proc.returncode != 0:
            raise HindsightError(f"the Hindsight plugin for Claude Code could not be installed: {_why(proc)}")
    absolute_hooks(ctx)
    # Written after the plugin install, which may have rewritten the file; keys it has are kept.
    current = _coding_agent(home)
    if current is None and coding_agent_path(home).exists():
        raise HindsightError(f"{coding_agent_path(home)} is not a JSON object; repair or remove it, then run this again")
    _write_private(coding_agent_path(home), json.dumps({**(current or {}), **coding_agent_values(ctx, key)},
                                                       indent=2) + "\n")


# ============================================================
# Step hindsight-timers: sync, snapshot, health, and the first sync
# ============================================================

def ledger_path(ctx) -> Path:
    """`ledger.jsonl` in the target data repository's `runtime:hindsight-sync/state`, asked of its registry."""
    proc = _run(ctx, [str(ctx.python), str(ctx.engine / "scripts" / "data-path.py"), "--data-root", str(ctx.data),
                      "path", "runtime:hindsight-sync/state"], cwd=ctx.engine, timeout=60)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise HindsightError(f"the data repository cannot place the sync ledger: {_why(proc)}")
    return Path(proc.stdout.strip().splitlines()[-1]) / "ledger.jsonl"


def ledger_has_sent(ledger: Path) -> bool:
    """At least one sent record in the ledger.

    Contract, read 2026-11-01 from scripts/hindsight-sync.py `cmd_push`: the sync
    PRINTS one JSON line per file the server accepted, with `path`, `sha256`,
    `operation_id` and `sent`, and writes no ledger itself; the caller appends
    stdout to the ledger, as hindsight-sync.service does (`>> "{{STATE}}/ledger.jsonl"`).
    """
    for line in (_read(ledger) or "").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("path") and rec.get("sent") and rec.get("operation_id"):
            return True
    return False


def check_timers(ctx) -> bool:
    if getattr(ctx, "dry_run", False):
        return False
    try:
        return (ctx.python.exists() and _services(ctx, "status", *TIMER_FAMILIES).returncode == 0
                and ledger_has_sent(ledger_path(ctx)))
    except HindsightError:
        return False


def run_timers(ctx) -> None:
    if _services(ctx, "install", *TIMER_FAMILIES, capture=False).returncode != 0:
        raise HindsightError("the service layer could not install " + ", ".join(TIMER_FAMILIES)
                             + "; see the lines above")
    ledger = ledger_path(ctx)
    if ledger_has_sent(ledger):
        return
    if not (check_service(ctx) and check_bank(ctx)):
        raise HindsightError("the first sync waits for the server and the bank main; run this again "
                             "once the steps before it are in place")
    proc = _run(ctx, [str(ctx.python), str(ctx.engine / "scripts" / "hindsight-sync.py"), "push", "--wave", "all",
                      "--limit", "50"], cwd=ctx.engine, timeout=1800,
                extra_env={"HEADING_OS_DATA": str(ctx.data), "HINDSIGHT_SYNC_API_URL": API_URL,
                           "HINDSIGHT_SYNC_BANK": BANK, "HINDSIGHT_SYNC_KEY_FILE": str(key_file(_home(ctx)))})
    if proc.returncode == 127 and not proc.stdout:
        raise HindsightError(f"the first sync could not run: {_why(proc)}")
    ledger.parent.mkdir(parents=True, exist_ok=True)
    if proc.stdout:
        with ledger.open("a", encoding="utf-8") as fh:  # append-only, as the timer appends
            fh.write(proc.stdout if proc.stdout.endswith("\n") else proc.stdout + "\n")
    if proc.returncode != 0:
        raise HindsightError(f"the first sync failed: {_why(proc)}")
