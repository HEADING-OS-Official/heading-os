#!/usr/bin/env python3
"""Herdr and CLIProxyAPI for `scripts/heading-install.py` (HOS-307, audit A7 and A8).

Slice 7 of the approved deployment design (registry name `plans`,
2026-11-01-heading-os-ideal-deployment-design, sections 2, 6 and 7). The
installer's `# ---- HOS-307` section registers one step per function pair
below; each `*_ready` reads the real state, each `install_*` repairs only what
that read found missing, and a rerun changes nothing that is in place.

What it leaves, relative to the executive's home:
  Herdr          ~/.local/bin/herdr (the release's own place), settings in
                 ~/.config/herdr/config.toml, the yard plugin linked from the
                 installed engine, the Claude Code integration installed
  CLIProxyAPI    ~/.local/share/heading-os/cliproxyapi/<version>/cli-proxy-api,
                 settings ~/.config/heading-os/cliproxyapi/config.yaml (mode 600),
                 logins in ~/.cli-proxy-api, the key as CLIPROXY_API_KEY and the
                 management panel's key as CLIPROXY_MANAGEMENT_KEY in the
                 engine's .env (mode 600), and the `cliproxyapi` service family
                 installed by the service layer (scripts/heading-services.py)

For Hindsight (HOS-304): `PROXY_BASE_URL` (re-exported from
scripts/utils/proxy_transport.py, the one place the port is fixed) and
`PROXY_KEY_ENV`, the `.env` name of the proxy key. Claude reaches the proxy
through the executive's own subscription, signed in by `-claude-login`.

Proof runs never touch the live machine: every check and run takes its home,
its `systemctl`, its `herdr`, its model catalog reader and its question prompt
from `host(ctx)`, which a harness replaces by setting `ctx.host`. The service
layer itself reads HOME and PATH, so the harness sets both for its children.

No secret is printed, logged or passed on a command line: the proxy key goes
from `secrets` into the config and `.env` inside one process, a provider key is
typed by the executive into `scripts/heading-secrets.py set`, and a provider
sign-in is the executive's own action in the browser.

Linux (systemd user units) is the tested path. macOS (launchd through the
service layer, `launchctl kickstart` for a restart) is written and UNTESTED.

Usage (the config tool, run by the installer through the engine's own Python,
which has PyYAML; it prints what it found or changed, never a value):
    python -m scripts.utils.install_herdr_cliproxy config check  CONFIG ENV_FILE
    python -m scripts.utils.install_herdr_cliproxy config repair CONFIG ENV_FILE
    python -m scripts.utils.install_herdr_cliproxy config kimi   CONFIG ENV_FILE
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.utils import install_person as person  # noqa: E402 - the person-needed stop (HOS-312)
from scripts.utils.install_runner import changed_since_start, runner_env, runner_problem, service_pid  # noqa: E402
from scripts.utils.paths import iter_env_pairs  # noqa: E402
from scripts.utils.proxy_transport import PROXY_BASE_URL  # noqa: E402,F401 - re-exported for HOS-304

PROXY_KEY_ENV = "CLIPROXY_API_KEY"
# CLIPROXY.md section 4.3 writes the Kimi key straight into config.yaml and
# names no variable for it, so the installer names one: the executive types the
# key into this .env name, and the config tool copies it into the proxy's
# Kimi block.
KIMI_KEY_ENV = "CLIPROXY_KIMI_API_KEY"
# The management panel's key in plaintext, which the executive types into the
# panel at http://127.0.0.1:<port>/management.html (HOS-312 round 2). The proxy
# itself keeps only a bcrypt hash of it once it has started.
MGMT_KEY_ENV = "CLIPROXY_MANAGEMENT_KEY"
PROXY_HOST = urlsplit(PROXY_BASE_URL).hostname
PROXY_PORT = urlsplit(PROXY_BASE_URL).port
AUTH_DIR = "~/.cli-proxy-api"  # the proxy's own default (config.example.yaml, oauth.auth-dir)

# Pinned to what the operator's machine ran on 2026-11-01 (`cli-proxy-api -help`
# banner and `herdr --version`, read only). The update manager may move the
# proxy later; that is accepted (HOS-307 decisions).
CLIPROXY_VERSION = "8.0.48"
HERDR_VERSION = "0.9.5"
HERDR_MIN = (0, 8, 0)  # the yard plugin's min_herdr_version
# Herdr publishes bare binaries and no checksum file in its GitHub release. Its
# SHA-256 values are published twice: the release manifest https://herdr.dev/latest.json
# (what its own install.sh verifies against) and the asset `digest` of the GitHub
# release API. The two agreed for v0.9.5 (MEASURED 2026-11-01), so they are
# pinned here and a download that differs is refused.
HERDR_SHA256 = {
    "linux-x86_64": "14d9e8bc8b4a9e0118d77c32266f76d461f09b40342eb23752960bd89f59929d",  # pragma: allowlist secret (a public release checksum)
    "linux-aarch64": "17d7536f2da50859523680ef279dffcac8e4a235f4e46fe8da6c2ec44141897c",  # pragma: allowlist secret (a public release checksum)
    "macos-x86_64": "5fb0a908ec7f6745e3e245738b06bfad56c46391c391ab3c0955fbead01f1369",  # pragma: allowlist secret (a public release checksum)
    "macos-aarch64": "fb77c2232a05a8035421382c894392f4174e4e38a18e48af3dd0290157c3a7e2",  # pragma: allowlist secret (a public release checksum)
}
HERDR_URL = "https://github.com/herdrdev/herdr/releases/download/v{version}/herdr-{target}"
HERDR_PLUGIN_ID = "heading-os.yard"
HERDR_TOAST = '[ui.toast]\ndelivery = "herdr"\n'

SERVICE = "cliproxyapi"
LAUNCHD_LABEL = "com.headingos." + SERVICE  # schedule.LAUNCHD_PREFIX + the service stem
LOGIN_WAIT_S = 30.0


class InstallError(OSError):
    """A plain sentence for the user. An OSError, so heading-install.py's step
    loop reports it as a failed step without a handler of its own."""


@dataclass(frozen=True)
class Provider:
    key: str
    title: str
    flag: str
    prefixes: tuple[str, ...] = ()
    names: tuple[str, ...] = ()
    required: bool = False

    def listed(self, models: list[str] | None) -> bool:
        return any(m.startswith(self.prefixes) or m in self.names for m in models or [])


# Order is the order they are offered (HOS-307 decisions). Claude is required:
# Hindsight reaches Sonnet through it (design section 6 item 4).
PROVIDERS = (
    Provider("claude", "Claude (your Claude subscription)", "-claude-login", ("claude-",), required=True),
    Provider("openai", "OpenAI (ChatGPT)", "-codex-login", ("gpt-",)),
    Provider("xai", "xAI (Grok)", "-xai-login", ("grok-",)),
    Provider("antigravity", "Google Antigravity (Gemini)", "-antigravity-login", ("gemini-",)),
)
KIMI = Provider("kimi", "Kimi (an API key from your Kimi for coding plan)", "",
                names=("kimi-for-coding", "k3", "k3.1-pro"))


# ============================================================
# The host a step acts on
# ============================================================

@dataclass
class Host:
    home: Path
    systemctl: str = "systemctl"
    herdr: str | None = None                                  # None: <home>/.local/bin/herdr
    models: Callable[[Path], list[str] | None] | None = None  # None: GET <PROXY_BASE_URL>/models
    ask: Callable[[str], bool] | None = None                  # None: a y/N question on the terminal


def host(ctx) -> Host:
    """`ctx.host` when a proof run set one, else this account."""
    return getattr(ctx, "host", None) or Host(home=Path.home())


def _injected(h: Host) -> str | None:
    """The proof run's stub service manager, or None on a real install."""
    return h.systemctl if h.systemctl != "systemctl" else None


def _env(ctx, h: Host) -> dict[str, str]:
    """HOME at this host's home; an injected systemctl's directory leads PATH, so a child
    such as the service layer reaches the stub and never this machine's user manager."""
    env = ctx.env()
    env["HOME"] = str(h.home)
    return runner_env(env, _injected(h))


def _run(cmd: list[str], env: dict[str, str] | None = None, cwd: Path | None = None,
         capture: bool = True) -> subprocess.CompletedProcess:
    """Never raises for a missing program: exit 127, as the service layer reports it."""
    try:
        return subprocess.run(cmd, env=env, cwd=str(cwd) if cwd else None, capture_output=capture,
                              text=True, errors="replace")
    except OSError as exc:
        return subprocess.CompletedProcess(cmd, 127, stdout="", stderr=str(exc))


def _write_private(path: Path, text: str) -> None:
    """Atomic, and private before its first byte (mkstemp creates mode 600)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _env_value(env_file: Path, name: str) -> str:
    try:
        text = env_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    for key, value in iter_env_pairs(text):
        if key == name:
            return value
    return ""


def _version(text: str) -> tuple[int, ...]:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(x) for x in m.groups()) if m else ()


def _arch() -> str:
    machine = platform.machine().lower()
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(machine)
    if not arch:
        raise InstallError(f"no release is built for this processor ({machine})")
    return arch


def _download(url: str, dest: Path) -> None:
    """The proxy updater's capped, https-only download (scripts/updaters/cliproxyapi_update.py)."""
    from scripts.updaters.cliproxyapi_update import _download as fetch
    try:
        fetch(url, dest)
    except (ValueError, urllib.error.URLError) as exc:
        raise InstallError(f"download failed: {url}: {exc}") from exc


def _sha256(path: Path) -> str:
    from scripts.updaters.cliproxyapi_update import _sha256 as digest
    return digest(path)


# ============================================================
# Herdr
# ============================================================

def herdr_bin(h: Host) -> Path:
    return Path(h.herdr) if h.herdr else h.home / ".local" / "bin" / "herdr"


def herdr_config(h: Host) -> Path:
    return h.home / ".config" / "herdr" / "config.toml"


def herdr_plugin_dir(engine: Path) -> Path:
    return engine / "scripts" / "herdr" / "heading-os-yard"


def _herdr(h: Host, ctx, *args: str) -> subprocess.CompletedProcess:
    return _run([str(herdr_bin(h)), *args], env=_env(ctx, h))


def herdr_ready(ctx) -> bool:
    """The program at its place, new enough, and settings that Herdr itself accepts."""
    h = host(ctx)
    if not os.access(herdr_bin(h), os.X_OK):
        return False
    probe = _herdr(h, ctx, "--version")
    if probe.returncode != 0 or _version(probe.stdout) < HERDR_MIN:
        return False
    cfg = herdr_config(h)
    if not cfg.is_file() or "[ui.toast]" not in cfg.read_text(encoding="utf-8", errors="replace"):
        return False
    return _herdr(h, ctx, "config", "check").returncode == 0


def install_herdr(ctx) -> None:
    h = host(ctx)
    binary = herdr_bin(h)
    probe = _herdr(h, ctx, "--version") if os.access(binary, os.X_OK) else None
    if probe is None or probe.returncode != 0 or _version(probe.stdout) < HERDR_MIN:
        osname = {"Linux": "linux", "Darwin": "macos"}.get(platform.system())
        target = f"{osname}-{_arch()}"
        want = HERDR_SHA256.get(target)
        if not osname or not want:
            raise InstallError(f"no Herdr {HERDR_VERSION} build is pinned for {target}")
        binary.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=binary.parent) as td:
            staged = Path(td) / "herdr"
            _download(HERDR_URL.format(version=HERDR_VERSION, target=target), staged)
            if _sha256(staged) != want:
                raise InstallError(f"the Herdr {HERDR_VERSION} download does not match its published "
                                   "checksum; nothing was installed")
            staged.chmod(0o755)
            os.replace(staged, binary)
        print(f"  Herdr {HERDR_VERSION} installed at {binary}")
    cfg = herdr_config(h)
    text = cfg.read_text(encoding="utf-8", errors="replace") if cfg.is_file() else None
    if text is None:
        _write_private(cfg, "# Herdr settings, written by the HEADING OS installer. Herdr's own defaults\n"
                            "# apply to everything not set here (herdr --default-config prints them).\n"
                            "# Notices inside Herdr: the yard plugin reports a ready or broken yard\n"
                            "# through them, and Herdr's default is off.\n" + HERDR_TOAST)
        print(f"  Herdr settings written: {cfg}")
    elif "[ui.toast]" not in text:
        _write_private(cfg, text + ("" if text.endswith("\n") else "\n") + "\n" + HERDR_TOAST)
        print(f"  Herdr settings: your file is kept; notices inside Herdr were added to {cfg}")
    else:
        print(f"  Herdr settings: your file is adopted as it is ({cfg})")
    check = _herdr(h, ctx, "config", "check")
    if check.returncode != 0:
        raise InstallError(f"Herdr does not accept {cfg}: {(check.stdout + check.stderr).strip()[-300:]}")


def _plugin_root(h: Host, ctx) -> str | None:
    """Where Herdr has the yard plugin linked from, or None."""
    out = _herdr(h, ctx, "plugin", "list", "--plugin", HERDR_PLUGIN_ID, "--json")
    if out.returncode != 0:
        return None
    try:
        plugins = json.loads(out.stdout)["result"]["plugins"]
    except (ValueError, KeyError, TypeError):
        return None
    for p in plugins if isinstance(plugins, list) else []:
        if isinstance(p, dict) and p.get("plugin_id") == HERDR_PLUGIN_ID and p.get("enabled") is True:
            return str(p.get("plugin_root") or "")
    return None


def _claude_integration_current(h: Host, ctx) -> bool:
    out = _herdr(h, ctx, "integration", "status")
    return out.returncode == 0 and any(line.strip().startswith("claude: current")
                                       for line in out.stdout.splitlines())


def herdr_plugin_ready(ctx) -> bool:
    """The yard plugin linked from THIS engine and enabled, and Herdr sees Claude Code."""
    h = host(ctx)
    if not os.access(herdr_bin(h), os.X_OK):
        return False
    return (_plugin_root(h, ctx) == str(herdr_plugin_dir(ctx.engine))
            and _claude_integration_current(h, ctx))


def install_herdr_plugin(ctx) -> None:
    """HERDR.md, "Link the yard plugin": a link, never a copy, so engine updates reach it."""
    h = host(ctx)
    want = str(herdr_plugin_dir(ctx.engine))
    root = _plugin_root(h, ctx)
    if root != want:
        if root is not None:
            print(f"  the yard plugin was linked from {root or 'another place'}; relinking it from this engine")
            _herdr(h, ctx, "plugin", "unlink", HERDR_PLUGIN_ID)
        out = _herdr(h, ctx, "plugin", "link", want)
        if out.returncode != 0:
            raise InstallError(f"herdr plugin link failed: {(out.stdout + out.stderr).strip()[-300:]}")
    if not _claude_integration_current(h, ctx):
        out = _herdr(h, ctx, "integration", "install", "claude")
        if out.returncode != 0:
            raise InstallError(f"herdr integration install claude failed: "
                               f"{(out.stdout + out.stderr).strip()[-300:]}")


# ============================================================
# CLIProxyAPI: the program
# ============================================================

def cliproxy_dir(home: Path) -> Path:
    return home / ".local" / "share" / "heading-os" / "cliproxyapi" / CLIPROXY_VERSION


def cliproxy_bin(home: Path) -> Path:
    return cliproxy_dir(home) / "cli-proxy-api"


def cliproxy_config(home: Path) -> Path:
    return home / ".config" / "heading-os" / "cliproxyapi" / "config.yaml"


def unit_values(home: Path) -> dict[str, str]:
    """The `{{CLIPROXY_*}}` placeholders of scripts/templates/systemd/cliproxyapi.service."""
    return {"CLIPROXY_BIN": str(cliproxy_bin(home)), "CLIPROXY_CONFIG": str(cliproxy_config(home)),
            "CLIPROXY_DIR": str(cliproxy_config(home).parent)}


def _cliproxy_asset() -> str:
    osname = {"Linux": "linux", "Darwin": "darwin"}.get(platform.system())
    if not osname:
        raise InstallError(f"CLIProxyAPI has no build for {platform.system()}")
    arch = {"x86_64": "amd64", "aarch64": "aarch64"}[_arch()]
    return f"CLIProxyAPI_{CLIPROXY_VERSION}_{osname}_{arch}.tar.gz"


def _cliproxy_version(binary: Path) -> tuple[int, ...]:
    """The `CLIProxyAPI Version:` banner `-help` prints (8.0.48 has no -version flag)."""
    out = _run([str(binary), "-help"])
    m = re.search(r"Version:\s*([0-9][0-9.]*)", (out.stdout or "") + (out.stderr or ""))
    return _version(m.group(1)) if m else ()


def cliproxy_ready(ctx) -> bool:
    """The pinned release or a newer one that runs: the update manager may move the proxy
    forward, and a rerun must not put 8.0.48 back over it (HOS-307 review round 3)."""
    binary = cliproxy_bin(host(ctx).home)
    if not os.access(binary, os.X_OK):
        return False
    found = _cliproxy_version(binary)  # () when the binary does not run or prints no banner
    return bool(found) and found >= _version(CLIPROXY_VERSION)


def install_cliproxy(ctx) -> None:
    """The release archive and its checksums.txt, verified as the updater verifies them."""
    from scripts.updaters.cliproxyapi_update import REPO, select_binary_member
    home = host(ctx).home
    binary, name = cliproxy_bin(home), _cliproxy_asset()
    base = f"https://github.com/{REPO}/releases/download/v{CLIPROXY_VERSION}/"
    binary.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=binary.parent.parent) as td:
        stage = Path(td)
        _download(base + name, stage / name)
        _download(base + "checksums.txt", stage / "checksums.txt")
        # `<sha256>  <asset name>` per line (the updater's contract, verified 2026-07-20).
        want = next((p[0] for p in (line.split() for line in
                                    (stage / "checksums.txt").read_text(encoding="utf-8").splitlines())
                     if len(p) == 2 and p[1] == name), None)
        if not want:
            raise InstallError(f"the CLIProxyAPI release lists no checksum for {name}; nothing was installed")
        if _sha256(stage / name) != want:
            raise InstallError(f"{name} does not match its published checksum; nothing was installed")
        try:
            with tarfile.open(stage / name) as tf:
                member, why = select_binary_member(tf.getmembers())
                if member is None:
                    raise InstallError(why)
                tf.extract(member, stage / "x", filter="data")
        except tarfile.TarError as exc:
            raise InstallError(f"{name} cannot be unpacked: {exc}") from exc
        new = stage / "x" / member.name
        new.chmod(0o755)
        os.replace(new, binary)
    print(f"  CLIProxyAPI {CLIPROXY_VERSION} installed at {binary}")


# ============================================================
# CLIProxyAPI: settings and key (the config tool runs in the engine's Python)
# ============================================================

def _config_tool(ctx, mode: str) -> subprocess.CompletedProcess:
    h = host(ctx)
    return _run([str(ctx.python), "-m", "scripts.utils.install_herdr_cliproxy", "config", mode,
                 str(cliproxy_config(h.home)), str(ctx.engine / ".env")],
                env=_env(ctx, h), cwd=ctx.engine)


def cliproxy_config_ready(ctx) -> bool:
    if not ctx.python.exists():
        return False
    return _config_tool(ctx, "check").returncode == 0


def install_cliproxy_config(ctx) -> None:
    out = _config_tool(ctx, "repair")
    for line in out.stdout.splitlines():
        print(f"  {line}")
    if out.returncode != 0:
        raise InstallError(f"the proxy settings could not be repaired: {out.stderr.strip()[-300:]}")


def _fresh_config(key: str, mgmt_key: str) -> str:
    """The v8 layout of CLIProxyAPI 8.0.48's config.example.yaml, only what HEADING OS sets.

    Serialized by PyYAML, never by interpolation: an adopted key holding a quote
    or a backslash must read back as exactly itself (HOS-307 review round 1).
    """
    import yaml

    data = {"config-version": 8,
            "server": {"host": PROXY_HOST, "port": PROXY_PORT},  # only this machine, never every interface
            "access": {"api-keys": [key]},
            "oauth": {"auth-dir": AUTH_DIR},  # each provider sign-in saves its token here
            "management": _management_values(mgmt_key)}
    return ("# CLIProxyAPI settings, written by the HEADING OS installer (scripts/heading-install.py).\n"
            "# This file holds the proxy key and the management panel's key, which the proxy\n"
            "# replaces with its bcrypt hash when it starts. It stays private (mode 600) and\n"
            "# outside every git repository. host is 127.0.0.1 so only this machine reaches the proxy, and\n"
            "# each provider sign-in saves its token in oauth.auth-dir. Every other setting\n"
            "# keeps the proxy's own default; the full list is config.example.yaml in the\n"
            "# CLIProxyAPI release.\n" + yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


def _management_values(mgmt_key: str) -> dict:
    """The management settings HEADING OS sets: the panel on (`disable-control-panel` left
    unset), its key, no updates of the panel from GitHub, and no management from another
    machine (`allow-remote`, config.example.yaml line 97 of 8.0.48)."""
    return {"allow-remote": False, "secret-key": mgmt_key, "disable-auto-update-panel": True}


def _bcrypt_form(value) -> bool:
    """The form 8.0.48 writes back in place of a plaintext key: `looksLikeBcrypt` of
    internal/config/config_validation.go lines 67 and 68 (prefix $2a$, $2b$ or $2y$),
    applied by config_load.go lines 245 to 254. A hash cannot be compared with .env."""
    return isinstance(value, str) and len(value) > 4 and value[:4] in ("$2a$", "$2b$", "$2y$")


def _management(data: dict, create: bool = False) -> dict:
    """The management section in the file's own layout: `management` in v8, the legacy
    `remote-management` that 8.0.48 maps onto it (internal/config/config_v8.go line 79)."""
    for name in ("management", "remote-management"):
        if isinstance(data.get(name), dict):
            return data[name]
    if not create:
        return {}
    section = data["remote-management" if _legacy(data) else "management"] = {}
    return section


def _management_plan(secret, env_value: str) -> tuple[str | None, str | None, str | None]:
    """(value for the settings' secret-key or None, value for .env or None, a line to print).

    The policy of HOS-312 round 2, by (settings, .env): absent/absent a new key into both;
    absent/present the .env value; plaintext/absent adopted into .env; plaintext/present and
    different the .env value wins; plaintext/same and hashed/present nothing; hashed/absent a
    new key into both, because the plaintext cannot be read back from a hash.
    """
    secret = secret if isinstance(secret, str) else ""
    if not secret:
        key = env_value or secrets.token_urlsafe(32)
        return key, (None if env_value else key), ("repaired: the management panel's key written into the settings"
                                                   + ("" if env_value else f" and into .env as {MGMT_KEY_ENV}"))
    if _bcrypt_form(secret):
        if env_value:
            return None, None, None
        key = secrets.token_urlsafe(32)
        return key, key, (f"the management panel's key was replaced: the settings hold only its hash, so the new "
                          f"key is in the settings and in .env as {MGMT_KEY_ENV}")
    if not env_value:
        return None, secret, f"repaired: the settings' management panel key adopted into .env as {MGMT_KEY_ENV}"
    if env_value != secret:
        return env_value, None, f"repaired: the management panel's key in the settings set to the engine's {MGMT_KEY_ENV}"
    return None, None, None


def _legacy(data: dict) -> bool:
    """The pre-v8 flat layout (top-level host, port, api-keys), which 8.0.48 still loads."""
    return "host" in data or "port" in data or isinstance(data.get("api-keys"), list)


def _slot(data: dict, v8: tuple[str, str], legacy: str, create: bool = False):
    """(container, key) of one setting in either layout."""
    if _legacy(data):
        return data, legacy
    section = data.get(v8[0])
    if not isinstance(section, dict):
        if not create:
            return {}, v8[1]
        section = data[v8[0]] = {}
    return section, v8[1]


def _problems(data, env_key: str, mgmt_env: str) -> list[str]:
    if not isinstance(data, dict):
        return ["the file does not parse as settings"]
    found = []
    box, k = _slot(data, ("server", "host"), "host")
    if box.get(k) != PROXY_HOST:
        found.append(f"it does not listen on {PROXY_HOST} only")
    box, k = _slot(data, ("server", "port"), "port")
    if box.get(k) != PROXY_PORT:
        found.append(f"its port is not {PROXY_PORT}")
    box, k = _slot(data, ("access", "api-keys"), "api-keys")
    keys = box.get(k) if isinstance(box.get(k), list) else []
    if not any(isinstance(x, str) and x for x in keys):
        found.append("it has no proxy key")
    elif not env_key:
        found.append(f"{PROXY_KEY_ENV} is not set in the engine's .env")
    elif env_key not in keys:
        found.append(f"the engine's {PROXY_KEY_ENV} is not one of its keys")
    box, k = _slot(data, ("oauth", "auth-dir"), "auth-dir")
    if not box.get(k):
        found.append("it names no folder for the sign-ins")
    mgmt = _management(data)
    secret = mgmt.get("secret-key")
    if not (isinstance(secret, str) and secret):
        found.append("it has no management block with a panel key")
    if mgmt.get("disable-auto-update-panel") is not True:
        found.append("the management panel would update itself from GitHub")
    if mgmt.get("allow-remote"):
        found.append("it allows management from other machines")
    if mgmt.get("disable-control-panel"):
        found.append("the management panel is switched off (disable-control-panel)")
    if not mgmt_env:
        found.append(f"{MGMT_KEY_ENV} is not set in the engine's .env")
    elif isinstance(secret, str) and secret and not _bcrypt_form(secret) and secret != mgmt_env:
        found.append(f"the settings' management panel key is not the engine's {MGMT_KEY_ENV}")
    return found


# The header Kimi's coding endpoint requires before it answers (a coding-agent client).
# Contract, read 2026-11-01 from the User-Agent line of the operator's working proxy
# config.yaml (legacy layout, CLIProxyAPI 8.0.48), the only line read from that file.
# Not the workspace User-Agent: the proxy sends it upstream, the engine sends nothing
# (exempted by name in config/user-agent-exemptions.json).
KIMI_USER_AGENT = "KimiCLI/0.77"


def _kimi_block(key: str) -> dict:
    """CLIPROXY.md section 4.3, in the v8 `api-keys.claude` form."""
    return {"name": "kimi", "base-url": "https://api.kimi.com/coding",
            "headers": {"User-Agent": KIMI_USER_AGENT}, "keys": [{"api-key": key}],
            "models": [{"name": n, "alias": n} for n in KIMI.names]}


def config_main(mode: str, config: Path, env_file: Path) -> int:
    """check: exit 0 when the settings are valid and agree with .env. repair: fix only what
    `check` finds, adopting what is valid, and say what was done. kimi: add the Kimi block
    from KIMI_KEY_ENV. Values are never printed."""
    import yaml  # the engine's environment; the installer's own Python has no PyYAML

    from scripts.utils.env_file import upsert_env_value
    env_key = _env_value(env_file, PROXY_KEY_ENV)
    mgmt_env = _env_value(env_file, MGMT_KEY_ENV)
    text = config.read_text(encoding="utf-8") if config.is_file() else None
    try:
        data = yaml.safe_load(text) if text is not None else None
    except yaml.YAMLError:
        data = "unparseable"
    if mode == "check":
        if text is None:
            print(f"absent: {config}")
            return 1
        found = _problems(data, env_key, mgmt_env)
        for p in found:
            print(f"invalid: {p}")
        # Both files hold the proxy key, so both must be private (HOS-307 review round 1).
        for path in (config, env_file):
            mode_bits = path.stat().st_mode & 0o777 if path.exists() else 0
            if mode_bits & 0o077:
                found.append(f"{path} is readable by others (mode {mode_bits:o})")
                print(f"invalid: {path} is readable by others (mode {mode_bits:o})")
        return 1 if found else 0

    if mode == "kimi":
        key = _env_value(env_file, KIMI_KEY_ENV)
        if not key or not isinstance(data, dict):
            print(f"not added: {KIMI_KEY_ENV} is not set, or the settings do not parse")
            return 1
        if _legacy(data):
            entries = [e for e in data.get("claude-api-key") or [] if isinstance(e, dict)]
            block = _kimi_block(key)
            entries = [e for e in entries if e.get("base-url") != block["base-url"]]
            entries.append({"api-key": key, "base-url": block["base-url"], "headers": block["headers"],
                            "models": block["models"]})
            data["claude-api-key"] = entries
        else:
            groups = data.get("api-keys") if isinstance(data.get("api-keys"), dict) else {}
            claude = [e for e in groups.get("claude") or [] if isinstance(e, dict) and e.get("name") != "kimi"]
            groups["claude"] = [*claude, _kimi_block(key)]
            data["api-keys"] = groups
        _write_private(config, yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
        print("added: the Kimi provider, with the key you typed")
        return 0

    # repair
    done = []
    if text is not None and not isinstance(data, dict):
        aside = config.with_name(f"{config.name}.unreadable-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")
        os.replace(config, aside)
        done.append(f"the old settings did not parse; kept as {aside.name}, new ones written")
        text, data = None, None
    if text is None:
        key = env_key or secrets.token_urlsafe(32)
        mgmt_key = mgmt_env or secrets.token_urlsafe(32)
        _write_private(config, _fresh_config(key, mgmt_key))
        done.append(f"settings written: {config}")
        if not mgmt_env:
            upsert_env_value(env_file, MGMT_KEY_ENV, mgmt_key)
            done.append(f"a new management panel key generated into the settings and into .env as {MGMT_KEY_ENV}")
        if not env_key:
            upsert_env_value(env_file, PROXY_KEY_ENV, key)
            done.append(f"a new proxy key generated into the settings and into .env as {PROXY_KEY_ENV}")
        else:
            done.append(f"the existing {PROXY_KEY_ENV} from .env is the proxy's key")
    else:
        found = _problems(data, env_key, mgmt_env)
        if not found:
            done.append(f"your settings are valid and adopted as they are: {config}")
        else:
            box, k = _slot(data, ("server", "host"), "host", create=True)
            if box.get(k) != PROXY_HOST:
                box[k] = PROXY_HOST
                done.append(f"repaired: it now listens on {PROXY_HOST} only")
            box, k = _slot(data, ("server", "port"), "port", create=True)
            if box.get(k) != PROXY_PORT:
                box[k] = PROXY_PORT
                done.append(f"repaired: port set to {PROXY_PORT}")
            box, k = _slot(data, ("access", "api-keys"), "api-keys", create=True)
            keys = [x for x in (box.get(k) if isinstance(box.get(k), list) else []) if isinstance(x, str) and x]
            if env_key and env_key not in keys:
                keys.append(env_key)
                done.append(f"repaired: the engine's {PROXY_KEY_ENV} added to the proxy's keys")
            elif not env_key:
                if not keys:
                    keys.append(secrets.token_urlsafe(32))
                    done.append("repaired: a new proxy key generated")
                upsert_env_value(env_file, PROXY_KEY_ENV, keys[0])
                done.append(f"repaired: the proxy's key written into .env as {PROXY_KEY_ENV}")
            box[k] = keys
            box, k = _slot(data, ("oauth", "auth-dir"), "auth-dir", create=True)
            if not box.get(k):
                box[k] = AUTH_DIR
                done.append(f"repaired: sign-ins are kept in {AUTH_DIR}")
            mgmt = _management(data, create=True)
            to_config, to_env, note = _management_plan(mgmt.get("secret-key"), mgmt_env)
            if to_config is not None:
                mgmt["secret-key"] = to_config
            if to_env is not None:
                upsert_env_value(env_file, MGMT_KEY_ENV, to_env)
            if note:
                done.append(note)
            if mgmt.get("disable-auto-update-panel") is not True:
                mgmt["disable-auto-update-panel"] = True
                done.append("repaired: the management panel no longer updates itself from GitHub")
            if mgmt.get("allow-remote"):
                mgmt["allow-remote"] = False
                done.append("repaired: management is allowed from this machine only")
            if mgmt.get("disable-control-panel"):
                # The only file setting that stops /management.html; Home.Enabled is not read
                # from the file (8.0.48 internal/api/server_management.go line 314, config.go line 39).
                del mgmt["disable-control-panel"]
                done.append("repaired: the management panel is switched on")
            backup = config.with_name(f"{config.name}.before-repair-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")
            _write_private(backup, text)
            _write_private(config, yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
            done.append(f"your previous file is kept as {backup.name} (its comments are not carried over)")
    config.chmod(0o600)
    config.parent.chmod(0o700)
    if env_file.exists():
        env_file.chmod(0o600)
    for line in done:
        print(line)
    return 0 if not _problems(yaml.safe_load(config.read_text(encoding="utf-8")),
                              _env_value(env_file, PROXY_KEY_ENV), _env_value(env_file, MGMT_KEY_ENV)) else 1


# ============================================================
# CLIProxyAPI: the service and the catalog
# ============================================================

def _services(ctx, verb: str, capture: bool = True) -> subprocess.CompletedProcess:
    """`scripts/heading-services.py <verb> cliproxyapi`, on the injected runner when there is one."""
    h = host(ctx)
    problem = runner_problem(_injected(h))
    if problem:
        raise InstallError(problem)
    return _run([str(ctx.python), str(ctx.engine / "scripts" / "heading-services.py"), verb, SERVICE],
                env=_env(ctx, h), cwd=ctx.engine, capture=capture)


def proxy_models(ctx) -> list[str] | None:
    """Model ids the proxy offers to the engine's key; None when it does not answer."""
    h = host(ctx)
    env_file = ctx.engine / ".env"
    if h.models is not None:
        return h.models(env_file)
    key = _env_value(env_file, PROXY_KEY_ENV)
    if not key:
        return None
    from scripts.utils.user_agent import user_agent
    req = urllib.request.Request(PROXY_BASE_URL + "/models",  # noqa: S310 - the fixed loopback address
                                 headers={"Authorization": f"Bearer {key}", "User-Agent": user_agent()})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:  # noqa: S310 - the fixed loopback address
            data = json.loads(resp.read().decode("utf-8")).get("data")
    except (urllib.error.URLError, OSError, ValueError, AttributeError):
        return None
    return [m["id"] for m in data if isinstance(m, dict) and m.get("id")] if isinstance(data, list) else None


def _wait_models(ctx, until: Callable[[list[str] | None], bool]) -> list[str] | None:
    deadline = time.monotonic() + LOGIN_WAIT_S
    while True:
        models = proxy_models(ctx)
        if until(models) or time.monotonic() >= deadline:
            return models
        time.sleep(1.0)


def cliproxy_service_ready(ctx) -> bool:
    """The service layer's own status (unit file, enabled, active) AND a real catalog answer."""
    if not ctx.python.exists() or _services(ctx, "status").returncode != 0:
        return False
    return proxy_models(ctx) is not None


def install_cliproxy_service(ctx) -> None:
    out = _services(ctx, "install", capture=False)
    if out.returncode != 0:
        raise InstallError("the service layer could not install cliproxyapi; "
                           "see the lines above (scripts/heading-services.py install cliproxyapi)")
    # Installing does not restart a running proxy, so settings the config step repaired
    # after it started would never apply (HOS-307 review round 3). An adopted unit is
    # never restarted: restart_proxy prints the command instead.
    h = host(ctx)
    if changed_since_start(cliproxy_config(h.home), service_pid(h.systemctl, f"{SERVICE}.service",
                                                                 LAUNCHD_LABEL, _env(ctx, h))):
        restart_proxy(ctx)
    if _wait_models(ctx, lambda m: m is not None) is None:
        raise InstallError(f"cliproxyapi runs but {PROXY_BASE_URL}/models does not answer with the "
                           f"engine's {PROXY_KEY_ENV}. If the line above says the unit was adopted, it is "
                           "not this installer's and was left untouched: point it at "
                           f"{cliproxy_config(host(ctx).home)}, or remove it, then run this again")


def _adopted_unit(h: Host) -> Path | None:
    """The cliproxyapi.service the service layer adopted (one it did not write), or None."""
    from scripts.utils.schedule import written_by_service_layer
    unit = h.home / ".config" / "systemd" / "user" / f"{SERVICE}.service"
    if unit.is_file() and not written_by_service_layer(unit):
        return unit
    return None


def restart_proxy(ctx) -> None:
    """Settings and a sign-in are read when the proxy starts (CLIPROXY.md section 8).

    An adopted unit is someone else's and is never restarted here (HOS-307 review
    round 1): the command is printed for the operator instead. A failed restart
    is an error, since the old process would go on answering on old settings.
    """
    h = host(ctx)
    if platform.system() == "Darwin":  # UNTESTED: no Mac was available
        out = _run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"], env=_env(ctx, h))
    else:
        adopted = _adopted_unit(h)
        if adopted is not None:
            print(f"  {adopted} was not written by HEADING OS and is left untouched, so it is not restarted. "
                  f"To load the new settings or sign-in, run yourself: systemctl --user restart {SERVICE}.service")
            return
        out = _run([h.systemctl, "--user", "restart", f"{SERVICE}.service"], env=_env(ctx, h))
    if out.returncode != 0:
        raise InstallError(f"cliproxyapi could not be restarted to load its settings: "
                           f"{(out.stdout + out.stderr).strip()[-300:]}")


# ============================================================
# CLIProxyAPI: the sign-ins, the executive's own action
# ============================================================

def _ask_terminal(question: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        answer = input(f"  {question} [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def _no_browser() -> bool:
    if platform.system() == "Darwin":
        return False
    try:
        wsl = "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except OSError:
        wsl = False
    return wsl or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def login_command(ctx, provider: Provider) -> list[str]:
    home = host(ctx).home
    cmd = [str(cliproxy_bin(home)), "-config", str(cliproxy_config(home)), provider.flag]
    return cmd + (["-no-browser"] if _no_browser() else [])


def _kimi_pending(ctx, models: list[str] | None) -> bool:
    """A Kimi key is in .env but the proxy offers none of Kimi's models yet."""
    return bool(_env_value(ctx.engine / ".env", KIMI_KEY_ENV)) and not KIMI.listed(models)


def signin_ready(ctx) -> bool:
    """Claude is in the proxy's catalog (the one sign-in the install requires), or the
    executive chose `--proxy-signin later` (HOS-312), and a Kimi key typed after an
    earlier run is not left unapplied (HOS-307 review round 1)."""
    models = proxy_models(ctx)
    claude_ok = _claude_later(ctx) or PROVIDERS[0].listed(models)
    return claude_ok and not _kimi_pending(ctx, models)


def _claude_later(ctx) -> bool:
    """The executive chose to connect Claude to the model proxy later (HOS-312)."""
    return getattr(ctx, "proxy_signin", None) == "later"


def _sign_in(ctx, provider: Provider) -> bool:
    cmd = login_command(ctx, provider)
    print(f"  Sign in to {provider.title} in your browser. Running now: {' '.join(cmd)}")
    if "-no-browser" in cmd:
        print("  It prints a web address: open it in any browser and sign in there.")
    _run(cmd, env=_env(ctx, host(ctx)), capture=False)
    restart_proxy(ctx)
    return provider.listed(_wait_models(ctx, provider.listed))


CLAUDE_SIGNIN = "signin-claude"
SIGNIN_WAIT_S = 15.0
CLAUDE_LATER = (" If you would rather do this later, say so, and I will run the installer with "
                "--proxy-signin later.")


def _claude_say(ctx, cmd: list[str]) -> str:
    """The same sentence on the first run and on every rerun while the sign-in is open."""
    url = person.first_url(person.log_tail(ctx, CLAUDE_SIGNIN))
    if url:
        return (f"Sign in to Claude in the browser page that opened. If no page opened, open this address: "
                f"{url}. Then tell me you are done." + CLAUDE_LATER)
    if "-no-browser" in cmd:
        return ("The Claude sign-in started but printed no address yet. Run the installer again in a moment."
                + CLAUDE_LATER)
    return "Sign in to Claude in the browser page that opened, then tell me you are done." + CLAUDE_LATER


def _claude_sign_in(ctx, claude: Provider) -> None:
    """Start `-claude-login` detached and stop for the person; on a later run, find it ended.

    Returns only when a sign-in that ended left Claude in the catalog (HOS-312).
    """
    h = host(ctx)
    cmd = login_command(ctx, claude)
    if person.running(ctx, CLAUDE_SIGNIN):
        raise person.WaitingForPerson(_claude_say(ctx, cmd))
    if person.ended(ctx, CLAUDE_SIGNIN):
        restart_proxy(ctx)
        listed = claude.listed(_wait_models(ctx, claude.listed))
        person.clear(ctx, CLAUDE_SIGNIN)
        if not listed:
            raise InstallError("the Claude sign-in ended but the proxy lists no Claude model. "
                               "Run the installer again to start a new sign-in.")
        print("  Claude: signed in")
        return
    person.start_detached(ctx, CLAUDE_SIGNIN, cmd, env=_env(ctx, h), cwd=ctx.engine)
    deadline = time.monotonic() + SIGNIN_WAIT_S
    while (person.first_url(person.log_tail(ctx, CLAUDE_SIGNIN)) is None
           and person.running(ctx, CLAUDE_SIGNIN) and time.monotonic() < deadline):
        time.sleep(0.5)
    raise person.WaitingForPerson(_claude_say(ctx, cmd))


def install_signins(ctx) -> None:
    h = host(ctx)
    ask = h.ask or _ask_terminal
    models = proxy_models(ctx)
    claude = PROVIDERS[0]
    if claude.listed(models):
        person.clear(ctx, CLAUDE_SIGNIN)
    elif _claude_later(ctx):
        # A sign-in still open from an earlier run is left alone, neither cleared nor stopped.
        print("  Claude for the model proxy: later, by your choice; "
              "connect it with heading-install.py --proxy-signin now")
    else:
        _claude_sign_in(ctx, claude)
    for provider in PROVIDERS[1:]:
        if provider.listed(proxy_models(ctx)):
            print(f"  {provider.title}: already signed in")
        elif ask(f"Sign in to {provider.title} now? Optional; answer no to skip."):
            print(f"  {provider.title}: " + ("signed in" if _sign_in(ctx, provider)
                                             else "the proxy lists none of its models yet; skipped"))
        else:
            print(f"  {provider.title}: skipped; later: {' '.join(login_command(ctx, provider))}")
    if KIMI.listed(proxy_models(ctx)):
        print(f"  {KIMI.title}: already connected")
    elif _kimi_pending(ctx, proxy_models(ctx)) or ask(f"Connect {KIMI.title}? Optional; answer no to skip."):
        if not _env_value(ctx.engine / ".env", KIMI_KEY_ENV):
            secrets_cmd = [str(ctx.python), str(ctx.engine / "scripts" / "heading-secrets.py"), "set", KIMI_KEY_ENV]
            if _run(secrets_cmd, env=_env(ctx, h), cwd=ctx.engine, capture=False).returncode != 0:
                print("  Kimi: no key was stored; skipped")
                return
        else:
            print(f"  Kimi: the key in .env ({KIMI_KEY_ENV}) is applied to the proxy")
        out = _config_tool(ctx, "kimi")
        print(f"  {out.stdout.strip() or out.stderr.strip()[-200:]}")
        restart_proxy(ctx)
        connected = KIMI.listed(_wait_models(ctx, KIMI.listed))
        print(f"  Kimi: {'connected' if connected else 'the proxy lists none of its models yet'}")
    else:
        print(f"  Kimi: skipped; later: .venv/bin/python scripts/heading-secrets.py set {KIMI_KEY_ENV}, "
              "then run this installer again")


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "config" and sys.argv[2] in ("check", "repair", "kimi"):
        raise SystemExit(config_main(sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4])))
    print(__doc__.split("Usage", 1)[1], file=sys.stderr)
    raise SystemExit(2)
