#!/usr/bin/env python3
"""doctor.py: is every component of this HEADING OS install working? Read only.

One line per check, in a plain table, and a non-zero exit on any red line, so
the installer can end only when doctor is all green (deployment design
§ 2 step 9, registry name `plans`, 2026-11-01-heading-os-ideal-deployment-design).

THE LESSON IT IS BUILT ON (HOS-294). A running process says nothing about
whether its jobs succeed: the Exchange sync daemon ran for days while every job
failed, and every surface called it healthy. So each module is checked twice,
its process AND the outcome of its last job, and a service unit is green only
when it is active and its last run exited 0.

IT REUSES THE READERS THAT EXIST, never a sibling of one:
  hooks          scripts/setup-platform.sh --check, scripts/install-hooks.py --check,
                 scripts/install-git-hooks.py --check
  mail           ops_signals.exchange_sync_from_log (last_job_ok / last_job_fail, HOS-294)
  restart loops  ops_signals.restart_loop_state
  local index    ops_signals.ollama_state, ops_signals.index_freshness_state
  proxy          council_freshness.probe_proxy (the /v1/models catalog)
  heartbeats     daemon-fleet-health.py's _collect_daemon_beats / _classify_beat
  tracker        scripts/lific-board.py (exit 2 when lific is present and a read fails)
  plugins        harness-audit.py's installed_plugins_path
What it adds is only what no reader had: one `systemctl --user show` per unit,
the Hindsight readiness probe at the address hindsight-sync.py uses, and the
table.

A component the installer has not put in place yet is a red line with a
one-line remedy. The remedy names `scripts/heading-install.py` when that
installer is in the checkout (HOS-301), else the per-component tool that exists
today.

Values of secrets are never read into the output: the secrets row reports
NAMES only, and the mode of `.env`.

HELM only. Inspecting whether a daemon runs is daemon business (engine
CLAUDE.md § HELM and YARD), so `main` refuses in a YARD like
daemon-fleet-health.py does. On a fresh install the clone is a main clone.

Usage:
    python scripts/doctor.py           # the table; exit 1 on any red line
    python scripts/doctor.py --json    # machine-readable rows, same exit code

Exit codes: 0 no red line (warnings allowed), 1 at least one red line,
2 refused (run from a YARD).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils import install_answers  # noqa: E402 - the one reader of the install's answers (HOS-312)
from scripts.utils.paths import iter_env_pairs  # noqa: E402

OK, WARN, RED = "ok", "warn", "red"
TIMEOUT_S = 60

#: Settings every install needs, by NAME. HEADING_OS_TZ: without it every timer
#: runs on UTC (voice.md § Language & Locale). CLIPROXY_API_KEY: the proxy's key
#: (council_freshness.probe_proxy). LIFIC_BOT_USER: the tracker's bot author
#: (scripts/utils/dossier.py BOT_ENV).
REQUIRED_SETTINGS = ("HEADING_OS_TZ", "CLIPROXY_API_KEY", "LIFIC_BOT_USER")
EXCHANGE_SETTINGS = ("EXCHANGE_EMAIL", "EXCHANGE_PASSWORD", "EXCHANGE_SERVER")
#: The mail units are installed by the installer's mail step, and only with
#: --mail exchange, so the generic "rerun the installer" remedy would not
#: install them (HOS-309).
MAIL_REMEDY = "heading-install.py --mail exchange"
#: The units the installer's mail step turns on; with HEADING_OS_MAIL=none in
#: .env (the executive chose no mail at install) they are absent by choice.
MAIL_UNITS = ("sync-exchange-daemon", "sentinel")

#: The module and fix of a unit whose installer is a module step, not the
#: generic service-layer command; every other unit doctor checks is named after
#: its family and fixed by `heading-services.py install <family>`.
UNIT_NOTES = {
    "hindsight-sync": ("hindsight", "bash scripts/install-hindsight-sync-timer.sh"),
    "hindsight-capture-check": ("hindsight", "bash scripts/install-hindsight-sync-timer.sh"),
    "hindsight-snapshot": ("hindsight", "bash scripts/install-hindsight-snapshot-timer.sh"),
    "memory-index-refresh": ("index", "bash scripts/install-memory-index-timer.sh"),
    "embedding-server": ("index", "the local index step of the installer"),
    "proxy-usage-dashboard": ("agentview", "bash scripts/install-proxy-usage-timer.sh"),
    "update-manager": ("cliproxy", "bash scripts/install-update-manager-timer.sh"),
    "daemon-watchdog": ("engine", "bash scripts/install-daemon-watchdog-timer.sh"),
    "ops-radar": ("engine", "bash scripts/install-ops-radar-timer.sh"),
    "reminders": ("engine", "bash scripts/install-reminders-timer.sh"),
    "sync-exchange-daemon": ("mail", MAIL_REMEDY),
    "sentinel": ("sentinel", MAIL_REMEDY),
    "lific": ("lific", ".venv/bin/python scripts/heading-services.py install lific"),
    "cliproxyapi": ("cliproxy", "the CLIProxyAPI step of the installer"),
    "hindsight-server": ("hindsight", "the Hindsight steps of the installer (scripts/heading-install.py)"),
    "hindsight-control-plane": ("hindsight", "the Hindsight steps of the installer (scripts/heading-install.py)"),
}


def unit_rows(platform: str) -> list[tuple[str, str, str, str]]:
    """`(module, unit, kind, fix)` for every unit an install turns on here.

    Read from the service layer, so doctor checks exactly what the installer's
    services step installs: every mandatory family that runs on `platform` and
    on this machine, plus the two Exchange mail families, which are optional
    but checked under the mail rule of `check_units`. One row per unit the
    family enables; `kind` timer: green when the timer is active and the
    service's last run exited 0; daemon: green when the service is active and
    running.
    """
    from scripts.utils.schedule import select_families

    fams = [f for f in select_families(mandatory_only=True)
            if platform in f.platforms and f.this_machine]
    fams += select_families(list(MAIL_UNITS))
    rows = []
    for fam in fams:
        for name in fam.enable_units:
            unit, _, suffix = name.rpartition(".")
            module, fix = UNIT_NOTES.get(
                unit, (fam.name, f".venv/bin/python scripts/heading-services.py install {fam.name}"))
            rows.append((module, unit, "timer" if suffix == "timer" else "daemon", fix))
    return rows


#: Plugins a module needs beyond the engine's own enabledPlugins.
MODULE_PLUGINS = ("agent-watch@heading-os-mods",)

UNIT_PROPS = ("LoadState", "ActiveState", "SubState", "Result",
              "ExecMainStatus", "ExecMainExitTimestampMonotonic",
              "LastTriggerUSec", "NextElapseUSecRealtime")


@dataclass
class Row:
    component: str
    check: str
    status: str
    detail: str
    remedy: str = ""


#: The checkout doctor's own code lives in. The readers it borrows are loaded
#: from here, never from the tree it inspects: a broken tree must still be read.
OWN_ROOT = Path(__file__).resolve().parent.parent


def _load_script(name: str):
    """A hyphenated engine script, loaded by path as ops_signals loads check-unit-drift.py."""
    spec = importlib.util.spec_from_file_location(
        "_doctor_" + name.replace("-", "_").removesuffix(".py"),
        OWN_ROOT / "scripts" / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _child_env() -> dict:
    """The environment for a child check, WITHOUT `WORKSPACE_ROOT`: an inherited
    one repoints every engine reader at another tree (engine CLAUDE.md § 8)."""
    env = dict(os.environ)
    env.pop("WORKSPACE_ROOT", None)
    return env


@dataclass
class Probes:
    """Everything doctor asks of the live machine, in one place, so a
    verification run can answer for a machine that is not there."""
    engine_root: Path
    data_root: Path | None
    platform: str = field(default_factory=lambda: sys.platform)

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def run(self, cmd: list[str], timeout: int = TIMEOUT_S) -> tuple[int, str]:
        """Exit code and combined output; 127 when the command cannot start,
        124 on timeout, so a check that did not run never reads as a pass."""
        try:
            proc = subprocess.run(cmd, cwd=str(self.engine_root), env=_child_env(),
                                  capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as exc:
            return 127, str(exc)
        except subprocess.TimeoutExpired:
            return 124, f"no answer in {timeout} s"
        return proc.returncode, (proc.stdout + proc.stderr).strip()

    def unit(self, name: str) -> dict | None:
        """`systemctl --user show` of one unit, or None when systemd cannot be asked."""
        rc, out = self.run(["systemctl", "--user", "show", name,
                            "-p", ",".join(UNIT_PROPS)], timeout=15)
        if rc != 0:
            return None
        return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)

    def http_ok(self, url: str, headers: dict[str, str] | None = None) -> tuple[bool, str]:
        if not url.startswith(("http://", "https://")):
            return False, f"not an http(s) address: {url}"
        from scripts.utils.user_agent import user_agent
        req = urllib.request.Request(url, headers={"User-Agent": user_agent(), **(headers or {})})  # noqa: S310 - scheme checked above
        try:
            with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - scheme checked above
                return r.status == 200, f"HTTP {r.status}"
        except (urllib.error.URLError, OSError) as exc:
            return False, str(exc)

    def proxy_models(self):
        from scripts.utils.council_freshness import probe_proxy
        return probe_proxy()

    def ollama(self) -> dict:
        from scripts.utils.ops_signals import ollama_state
        return ollama_state()

    def index(self) -> dict:
        from scripts.utils.ops_signals import index_freshness_state
        return index_freshness_state(self.engine_root, self.data_root)

    def index_sample(self) -> dict | None:
        """One indexed chunk to ask the index for (scripts/utils/install_index.py, HOS-305)."""
        from scripts.utils.install_index import sample_chunk
        return sample_chunk(self.engine_root, self.data_root)

    def exchange(self, unit_present: bool) -> dict:
        from scripts.utils.ops_signals import exchange_sync_from_log
        from scripts.utils.sync_exchange_paths import daemon_files
        return exchange_sync_from_log(daemon_files(create=False).log, unit_present)

    def google(self, engine_root: Path) -> list[dict]:
        """`status()` of scripts/google-connect.py for `engine_root`; never opens a browser (HOS-324)."""
        return _load_script("google-connect.py").status(engine_root)

    def restart_loops(self) -> dict:
        from scripts.utils.ops_signals import restart_loop_state
        return restart_loop_state()

    def beats(self) -> list[tuple[str, str, str]]:
        """(daemon, ok|stale|error, detail) per present heartbeat."""
        fleet = _load_script("daemon-fleet-health.py")
        return [(str(b.get("daemon", "?")), fleet._classify_beat(b, fleet.STALE_DEFAULT_S),
                 str(b.get("detail") or ""))
                for b in fleet._collect_daemon_beats()]

    def installed_plugins(self) -> set[str] | None:
        path = _load_script("harness-audit.py").installed_plugins_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        plugins = data.get("plugins") if isinstance(data, dict) else None
        return set(plugins) if isinstance(plugins, dict) else None


def _remedy(engine_root: Path, fallback: str) -> str:
    """The one-line fix: the resumable installer once it ships, else `fallback`."""
    if (engine_root / "scripts" / "heading-install.py").is_file():
        return ".venv/bin/python scripts/heading-install.py (it resumes and repairs)"
    return fallback


def _settings(engine_root: Path) -> dict[str, str] | None:
    """NAME -> value from the engine's .env, or None when there is none. The
    values stay inside this process; only names and emptiness reach a row."""
    try:
        text = (engine_root / ".env").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    found: dict[str, str] = {}
    for key, value in iter_env_pairs(text):
        found.setdefault(key, value)
    return found


def _setting(env: dict[str, str] | None, name: str, default: str = "") -> str:
    """The process environment first, then .env: the order `load_api_key` uses."""
    return os.environ.get(name) or (env or {}).get(name) or default


# ------------------------------------------------------------ the checks

def check_engine(p: Probes) -> list[Row]:
    root = p.engine_root
    rows = []
    missing = [n for n in ("CLAUDE.md", ".claude", "scripts") if not (root / n).exists()]
    rows.append(Row("engine", "checkout", RED if missing else OK,
                    f"missing: {', '.join(missing)}" if missing else str(root),
                    "" if not missing else _remedy(root, "clone the engine again")))
    if not (root / ".git").exists():
        rows.append(Row("engine", "git", RED, "not a git checkout",
                        _remedy(root, "clone the engine with git")))
    py = root / ".venv" / "bin" / "python"
    if not py.exists():
        rows.append(Row("engine", "python env", RED, ".venv/bin/python is absent",
                        _remedy(root, "uv sync --all-extras")))
    else:
        rc, out = p.run([str(py), "--version"], timeout=15)
        good = rc == 0 and out.startswith("Python 3.11")
        rows.append(Row("engine", "python env", OK if good else RED,
                        out.splitlines()[0] if out else f"exit {rc}",
                        "" if good else _remedy(root, "uv sync --all-extras")))
    return rows


def check_data(p: Probes) -> list[Row]:
    d = p.data_root
    if d is None or not d.is_dir():
        return [Row("data", "repository", RED, "no data repository found",
                    _remedy(p.engine_root, ".venv/bin/python scripts/create-data-repo.py"))]
    if not (d / ".git").exists():
        return [Row("data", "repository", RED, f"{d} is not a git repository",
                    _remedy(p.engine_root, f"git init {d}"))]
    return [Row("data", "repository", OK, str(d))]


def check_hooks(p: Probes) -> list[Row]:
    root = p.engine_root
    py = str(root / ".venv" / "bin" / "python")
    checks = (
        ("session hooks", ["bash", str(root / "scripts" / "setup-platform.sh"), "--check"],
         "bash scripts/setup-platform.sh"),
        ("commit gate", [py, str(root / "scripts" / "install-hooks.py"), "--check"],
         "pre-commit install"),
        ("push gate", [py, str(root / "scripts" / "install-git-hooks.py"), "--check"],
         ".venv/bin/python scripts/install-git-hooks.py"),
    )
    rows = []
    for name, cmd, fix in checks:
        if not Path(cmd[1]).is_file():
            rows.append(Row("hooks", name, RED, f"{Path(cmd[1]).name} is absent",
                            _remedy(root, "clone the engine again")))
            continue
        rc, out = p.run(cmd)
        last = out.splitlines()[-1] if out else f"exit {rc}"
        rows.append(Row("hooks", name, OK if rc == 0 else RED,
                        "armed" if rc == 0 else last[:160], "" if rc == 0 else _remedy(root, fix)))
    return rows


def check_secrets(p: Probes, env: dict[str, str] | None) -> list[Row]:
    root = p.engine_root
    path = root / ".env"
    if env is None:
        return [Row("secrets", ".env", RED, ".env is absent or unreadable",
                    _remedy(root, "cp .env.example .env && chmod 600 .env"))]
    rows = []
    mode = path.stat().st_mode & 0o777
    rows.append(Row("secrets", ".env mode", OK if mode & 0o077 == 0 else RED, f"{mode:o}",
                    "" if mode & 0o077 == 0 else "chmod 600 .env"))
    tool = (root / "scripts" / "heading-secrets.py").is_file()
    absent = [n for n in REQUIRED_SETTINGS if not _setting(env, n)]
    rows.append(Row("secrets", "required names", RED if absent else OK,
                    f"not set: {', '.join(absent)}" if absent else ", ".join(REQUIRED_SETTINGS),
                    "" if not absent else (
                        f".venv/bin/python scripts/heading-secrets.py set {absent[0]}" if tool
                        else f"set {', '.join(absent)} in .env")))
    return rows


def _launchd_row(p: Probes, module: str, unit: str, kind: str, installer: str) -> Row:
    """One unit on macOS, read from launchd (HOS-341; UNTESTED on a Mac)."""
    from scripts.utils.schedule import SERVICE_FAMILIES, _launchd_labels, launchd_healthy, launchd_status

    name = f"{unit}.timer" if kind == "timer" else f"{unit}.service"
    fix = installer if installer == MAIL_REMEDY else _remedy(p.engine_root, installer)
    fam = next((f for f in SERVICE_FAMILIES if name in f.enable_units), None)
    if fam is None:
        return Row(module, unit, RED, f"no service family installs {name}", fix)
    label = _launchd_labels(fam)[fam.enable_units.index(name)]
    st = launchd_status(label)
    if not st["loaded"]:
        return Row(module, unit, RED, f"{label} is not loaded", fix)
    exit_text = (("never ran" if kind == "timer" else "never exited") if st["last_exit"] is None
                 else f"last exit {st['last_exit']}")
    good = launchd_healthy(st, timer=kind == "timer")
    if kind == "timer":
        return Row(module, unit, OK if good else RED, f"{label} loaded, {exit_text}", "" if good else fix)
    return Row(module, unit, OK if good else RED,
               f"{label} {'running' if good else 'loaded but not running'}, {exit_text}",
               "" if good else f"launchctl kickstart -k gui/$(id -u)/{label}")


def _unit_row(p: Probes, module: str, unit: str, kind: str, installer: str) -> Row:
    if p.platform == "darwin":
        return _launchd_row(p, module, unit, kind, installer)
    if not p.platform.startswith("linux"):
        return Row(module, unit, RED, f"doctor reads no service manager on {p.platform} yet",
                   "the launchd half of the service layer (HOS-303)")
    name = f"{unit}.timer" if kind == "timer" else f"{unit}.service"
    state = p.unit(name)
    fix = installer if installer == MAIL_REMEDY else _remedy(p.engine_root, installer)
    if state is None:
        from scripts.utils.schedule import WSL_SYSTEMD_FIX
        return Row(module, unit, RED, "systemd user session not reachable",
                   f"enable systemd user sessions (WSL: {WSL_SYSTEMD_FIX}, then wsl --shutdown)")
    if state.get("LoadState") != "loaded":
        return Row(module, unit, RED, f"{name} not installed", fix)
    if state.get("ActiveState") != "active":
        return Row(module, unit, RED, f"{name} is {state.get('ActiveState')}", fix)
    if kind == "daemon":
        sub = state.get("SubState")
        return Row(module, unit, OK if sub == "running" else RED, f"{name} active ({sub})",
                   "" if sub == "running" else f"systemctl --user restart {name}")
    svc = p.unit(f"{unit}.service")
    # A timer can be active over a service that is gone; a missing exit
    # timestamp then means "cannot run", never "not run yet".
    if svc is None:
        return Row(module, unit, RED, f"{unit}.service could not be queried", fix)
    if svc.get("LoadState") != "loaded":
        return Row(module, unit, RED,
                   f"timer active but {unit}.service is {svc.get('LoadState') or 'unknown'}", fix)
    # A failed Result is a failure even when the main process never ran
    # (an ExecStartPre exit leaves no main exit timestamp).
    if svc.get("Result") not in (None, "", "success"):
        return Row(module, unit, RED,
                   f"last run failed: result={svc.get('Result')} status={svc.get('ExecMainStatus')}",
                   f"journalctl --user -u {unit}.service -n 50")
    if svc.get("ExecMainExitTimestampMonotonic", "0") in ("", "0"):
        # A timer that has never fired and names its first run is waiting, not
        # failing; anything else stays a WARN. The value is shown as systemd prints it.
        nxt = state.get("NextElapseUSecRealtime", "")
        if state.get("LastTriggerUSec", "") in ("", "n/a", "0") and nxt not in ("", "n/a"):
            return Row(module, unit, OK, f"waiting for its first run at {nxt}")
        return Row(module, unit, WARN, "timer active, not run yet")
    good = svc.get("Result") == "success" and svc.get("ExecMainStatus") == "0"
    return Row(module, unit, OK if good else RED,
               "timer active, last run exited 0" if good else
               f"last run failed: result={svc.get('Result')} status={svc.get('ExecMainStatus')}",
               "" if good else f"journalctl --user -u {unit}.service -n 50")


def _mail_by_choice_off(engine_root: Path) -> bool:
    """True when the executive chose no mail at install (HEADING_OS_MAIL=none)."""
    return install_answers.mail(engine_root) == "none"


def _proxy_by_choice_later(engine_root: Path) -> bool:
    """True when the executive left the Claude sign-in for the model proxy for later
    (HEADING_OS_PROXY_SIGNIN=later, HOS-312)."""
    return install_answers.proxy_signin(engine_root) == "later"


BY_CHOICE = "not connected, by choice"


def _mail_is_google(engine_root: Path) -> bool:
    """True when the executive connected Google mail at install (HEADING_OS_MAIL=google, HOS-324)."""
    return install_answers.mail(engine_root) == "google"


#: With Google mail the two Exchange units stay absent. WARN by HELM decision
#: 2026-11-02, not by a measurement: Google has no background sync and no
#: Sentinel source yet.
GOOGLE_UNIT_NOTE = "not used with Google mail; Google has no background sync yet (HOS-324)"


def check_units(p: Probes, env: dict[str, str] | None) -> list[Row]:
    off = _mail_by_choice_off(p.engine_root)
    google = _mail_is_google(p.engine_root)
    return [Row(u[0], u[1], OK, BY_CHOICE) if off and u[1] in MAIL_UNITS
            else Row(u[0], u[1], WARN, GOOGLE_UNIT_NOTE) if google and u[1] in MAIL_UNITS
            else _unit_row(p, *u) for u in unit_rows(p.platform)]


GOOGLE_CLIENT_REMEDY = (".venv/bin/python scripts/heading-install.py --mail google "
                        "--google-client <path to the downloaded file>")
GOOGLE_AUDIENCE_REMEDY = ("set the app to In production on the Google console's Audience page, then: "
                          ".venv/bin/python scripts/heading-install.py --mail google --google-audience production")
GOOGLE_CONSENT_REMEDY = ".venv/bin/python scripts/google-connect.py consent (in your own terminal)"


def check_google(p: Probes, env: dict[str, str] | None) -> list[Row]:
    """The executive's own Google sign-in app, when mail is Google (HOS-324).

    The audience row is the executive's own reading of the Google console, recorded
    by the installer; it is not measured. No row carries an address, a client id
    or a token.
    """
    if not _mail_is_google(p.engine_root):
        return []
    audience = _setting(env, "HEADING_OS_GOOGLE_AUDIENCE")
    rows = []
    try:
        items = {it["part"]: it for it in p.google(p.engine_root)}
    except (OSError, ImportError, SyntaxError, ValueError, RuntimeError, AttributeError, KeyError, TypeError) as exc:
        return [Row("google", "client file", RED, f"Google status not read ({type(exc).__name__})",
                    GOOGLE_CLIENT_REMEDY)]
    client = items.get("client", {})
    rows.append(Row("google", "client file", OK if client.get("ok") else RED,
                    str(client.get("reason", "not reported"))[:160],
                    "" if client.get("ok") else GOOGLE_CLIENT_REMEDY))
    good = audience in ("production", "internal")
    rows.append(Row("google", "app audience", OK if good else RED,
                    f"{audience}, as you read it in the Google console" if good else
                    "not recorded; read the Google console's Audience page",
                    "" if good else GOOGLE_AUDIENCE_REMEDY))
    for part, check in (("gmail", "gmail sign-in"), ("calendar", "calendar sign-in")):
        it = items.get(part, {})
        rows.append(Row("google", check, OK if it.get("ok") else RED,
                        str(it.get("reason", "not reported"))[:160],
                        "" if it.get("ok") else GOOGLE_CONSENT_REMEDY))
    return rows


def _layer_collection(layer: str) -> str | None:
    """The collection memory-index queries `layer` under, or None when no collection holds it.

    Read from the `collections` map of config/memory-index.yaml, the map
    memory-index.py's `load_config` loads as it stands (it only defaults the
    key) and `cmd_query` routes `--collection` by. Read from OWN_ROOT, the tree
    whose memory-index.py the round trip runs, so the map and the query agree.
    The first collection listing the layer wins, as `_layer_store_map` refuses
    a layer in two stores. Raises OSError or ValueError when the file cannot
    be read.
    """
    import yaml
    cfg = yaml.safe_load((OWN_ROOT / "config" / "memory-index.yaml").read_text(encoding="utf-8")) or {}
    colls = cfg.get("collections") if isinstance(cfg, dict) else None
    if not isinstance(colls, dict):
        raise ValueError("config/memory-index.yaml has no collections map")
    return next((name for name, layers in colls.items() if isinstance(layers, list) and layer in layers), None)


def _index_roundtrip(p: Probes) -> Row:
    """A real query: the text of one indexed chunk must come back with that chunk first (HOS-305).

    Asked under the collection the chunk's own layer belongs to: on a fresh
    install the only content row is the data repository's first commit, layer
    `commit-data`, collection `history`, and `--collection content` cannot
    return it (measured on a fresh guest, HOS-312, 2026-11-03).
    """
    build = ".venv/bin/python scripts/memory-index.py build"
    import sqlite3
    import yaml
    try:
        sample = p.index_sample()
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        return Row("index", "query", RED, f"index store not read: {exc}"[:160], build)
    if sample is None:
        return Row("index", "query", RED, "the index is empty", _remedy(p.engine_root, build))
    try:
        collection = _layer_collection(sample["layer"])
    except (OSError, ValueError, yaml.YAMLError) as exc:
        return Row("index", "query", RED, f"collections map not read: {exc}"[:160],
                   "check config/memory-index.yaml")
    if collection is None:
        return Row("index", "query", RED,
                   f"layer {sample['layer']} of {sample['path']} is in no collection"[:160],
                   "check the collections map of config/memory-index.yaml")
    rc, out = p.run([str(OWN_ROOT / ".venv" / "bin" / "python"), str(OWN_ROOT / "scripts" / "memory-index.py"),
                     "query", sample["text"], "--collection", collection, "--top-k", "1", "--json"])
    hits: list = []
    # `run` joins stdout and stderr: the payload is the last line that parses as an object.
    for line in reversed(out.splitlines() if rc == 0 else []):
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            hits = payload.get("hits") or []
            break
    first = hits[0] if hits and isinstance(hits[0], dict) else {}
    where = f"{sample['path']}#{sample['chunk']}"
    if first.get("path") == sample["path"] and first.get("chunk") == sample["chunk"]:
        return Row("index", "query", OK, f"{where} found itself first ({collection})"[:160])
    got = f"{first.get('path')}#{first.get('chunk')}" if first else (out.splitlines() or [f"exit {rc}"])[-1]
    return Row("index", "query", RED, f"asked for {where}, got {got}"[:160],
               "start the embedding server, then " + build)


def check_modules(p: Probes, env: dict[str, str] | None) -> list[Row]:
    root = p.engine_root
    rows = []

    # Hindsight: the server answers a request carrying its key, and the key file exists
    # (hindsight-sync.py's defaults).
    api = _setting(env, "HINDSIGHT_SYNC_API_URL", "http://127.0.0.1:8888").rstrip("/")
    key = Path(_setting(env, "HINDSIGHT_SYNC_KEY_FILE",
                        str(Path.home() / ".config/hindsight/secrets/hindsight-api-key"))).expanduser()
    try:
        secret = key.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        secret = ""
    ok, why = p.http_ok(f"{api}/health/ready", {"Authorization": f"Bearer {secret}"} if secret else None)
    rows.append(Row("hindsight", "server", OK if ok else RED,
                    f"{api} ready" if ok else f"{api}: {why}"[:160],
                    "" if ok else _remedy(root, "start the Hindsight server (docs: Hindsight setup)")))
    rows.append(Row("hindsight", "key file", OK if key.is_file() else RED,
                    "present" if key.is_file() else f"{key} is absent",
                    "" if key.is_file() else _remedy(root, "write the Hindsight key file")))
    # The bank main, as the command line lists it, and the extraction model through the proxy (HOS-304).
    from scripts.utils.install_hindsight import (ENV_LLM_BASE_URL, ENV_LLM_MODEL, PROXY_BASE_URL,
                                                 bank_listed, read_server_env, server_env_path)
    rc, out = p.run(["hindsight", "bank", "list", "-o", "json"])
    listed = rc == 0 and bank_listed(out)
    rows.append(Row("hindsight", "bank", OK if listed else RED,
                    "main listed" if listed else ("main is not listed" if rc == 0 else
                                                  (out.splitlines() or [f"exit {rc}"])[-1][:160]),
                    "" if listed else _remedy(root, "hindsight bank create main --name \"HEADING OS\"")))
    server_env = read_server_env(Path.home())
    model, base = server_env.get(ENV_LLM_MODEL, ""), server_env.get(ENV_LLM_BASE_URL, "")
    extraction = model.startswith("claude-") and base == PROXY_BASE_URL
    proxy_later = _proxy_by_choice_later(root)
    if proxy_later and not extraction:
        rows.append(Row("hindsight", "extraction", OK, BY_CHOICE))
    else:
        rows.append(Row("hindsight", "extraction", OK if extraction else RED,
                        f"{model} through {base}" if extraction else
                        f"model {model or 'none'} at {base or 'no base URL'}, in {server_env_path(Path.home())}"[:160],
                        "" if extraction else _remedy(root, "write the Hindsight server settings")))
    # The web console, at its address in the one list of web interfaces (HOS-312).
    from scripts.utils.web_interfaces import answers, web_interfaces

    console = {wi.name: wi.url for wi in web_interfaces()}["Hindsight"]
    up = answers(console)
    rows.append(Row("hindsight", "web console", OK if up else RED,
                    f"{console} answers" if up else f"{console} does not answer",
                    "" if up else _remedy(root, ".venv/bin/python scripts/heading-install.py")))

    # Local index: an embedding server answers, and the index is fresh.
    try:
        oll = p.ollama()
        idx = p.index()
    except (OSError, ValueError, RuntimeError) as exc:
        rows.append(Row("index", "embedding + index", RED, f"not checked: {exc}"[:160],
                        _remedy(root, "install the embedding server and run scripts/memory-index.py")))
    else:
        rows.append(Row("index", "embedding server", RED if oll.get("due") else OK,
                        str(oll.get("summary", ""))[:160],
                        _remedy(root, "start the embedding server") if oll.get("due") else ""))
        sev = idx.get("severity", "ok")
        rows.append(Row("index", "index", RED if sev in ("high", "critical") else WARN if idx.get("due") else OK,
                        str(idx.get("summary", ""))[:160],
                        ".venv/bin/python scripts/memory-index.py build" if idx.get("due") else ""))
    rows.append(_index_roundtrip(p))

    # Lific: the binary, and a read through lific-board.py.
    if not p.which("lific"):
        rows.append(Row("lific", "binary", RED, "lific is not on PATH",
                        _remedy(root, "install Lific (docs: Lific setup)")))
    else:
        rc, out = p.run([str(OWN_ROOT / ".venv" / "bin" / "python"),
                         str(OWN_ROOT / "scripts" / "lific-board.py"), "--json"])
        rows.append(Row("lific", "tracker read", OK if rc == 0 else RED,
                        "board read" if rc == 0 else (out.splitlines() or [f"exit {rc}"])[-1][:160],
                        "" if rc == 0 else "systemctl --user status lific.service"))
    # Lific's server answers a real request, and Claude Code has it registered
    # (read from the Claude Code user config, never `claude mcp list`, which starts servers).
    from scripts.utils.install_lific import claude_config_path, health_url, registered_command, server_answers
    url = health_url(Path.home())
    up = server_answers(Path.home())
    rows.append(Row("lific", "server", OK if up else RED,
                    f"{url} answers" if up else f"no answer at {url or 'the address in lific.toml'}",
                    "" if up else "systemctl --user status lific.service"))
    cmd = registered_command(Path.home())
    present = isinstance(cmd, str) and bool(cmd) and os.access(cmd, os.X_OK)
    rows.append(Row("lific", "MCP registration", OK if present else RED,
                    f"registered: {cmd}" if present else
                    (f"registered command {cmd} is missing" if cmd else
                     f"no lific server in {claude_config_path(Path.home())}"),
                    "" if present else _remedy(root, "register Lific with Claude Code (docs: Lific setup)")))

    # CLIProxyAPI: the catalog answers with the key.
    try:
        models = p.proxy_models()
    except (OSError, ValueError, RuntimeError) as exc:
        models, why = None, str(exc)
    else:
        why = "no answer, or CLIPROXY_API_KEY is not set"
    # With the Claude sign-in left for later (HOS-312), an empty catalog is the
    # executive's choice: the proxy serves no model until Claude is connected.
    if proxy_later and not models:
        rows.append(Row("cliproxy", "model catalog", OK, BY_CHOICE))
    else:
        rows.append(Row("cliproxy", "model catalog", OK if models else RED,
                        f"{len(models)} models" if models else why[:160],
                        "" if models else _remedy(root, "start cliproxyapi.service and set CLIPROXY_API_KEY")))
    # Claude in that catalog: Hindsight reaches Sonnet through it (design section 6 item 4, HOS-307).
    claude = [m for m in models or [] if m.startswith("claude-")]
    if proxy_later and not claude:
        rows.append(Row("cliproxy", "Claude sign-in", OK, BY_CHOICE))
    else:
        rows.append(Row("cliproxy", "Claude sign-in", OK if claude else RED,
                        f"{len(claude)} Claude models" if claude else "the catalog lists no claude- model",
                        "" if claude else _remedy(root, "cli-proxy-api -config <its config.yaml> -claude-login")))

    # Herdr: on PATH, it answers, and the yard plugin is linked from this engine (HOS-307).
    herdr = p.which("herdr")
    if not herdr:
        rows.append(Row("herdr", "binary + yard plugin", RED, "herdr is not on PATH",
                        _remedy(root, "install Herdr (docs: Herdr setup)")))
    else:
        rc, out = p.run([herdr, "--version"], timeout=15)
        plugin = root / "scripts" / "herdr" / "heading-os-yard"
        prc, pout = p.run([herdr, "plugin", "list", "--plugin", "heading-os.yard", "--json"], timeout=15)
        try:
            roots = [x.get("plugin_root") for x in json.loads(pout)["result"]["plugins"]
                     if isinstance(x, dict) and x.get("enabled") is True] if prc == 0 else []
        except (ValueError, KeyError, TypeError):
            roots = []
        good = rc == 0 and str(plugin) in roots
        rows.append(Row("herdr", "binary + yard plugin", OK if good else RED,
                        (out.splitlines() or ["?"])[0][:60] + ", plugin linked" if good else
                        f"herdr --version exited {rc}" if rc != 0 else f"yard plugin not linked from {plugin}",
                        "" if good else _remedy(root, f"herdr plugin link {plugin}")))

    # AgentsView's binary, at the versioned path the installer puts it and the unit
    # runs; never on PATH, so `which` cannot find it. Its server has its own row.
    from scripts.utils.install_agentview import URL, agent_watch_state, binary_path, server_answers
    binary = binary_path(Path.home())
    found = binary.is_file() and os.access(binary, os.X_OK)
    rows.append(Row("agentview", "binary", OK if found else RED,
                    str(binary) if found else f"{binary} is missing",
                    "" if found else _remedy(root, ".venv/bin/python scripts/heading-install.py")))
    # AgentsView's server answers, and agent-watch is enabled from this engine (HOS-308).
    up = server_answers()
    rows.append(Row("agentview", "server", OK if up else RED,
                    f"{URL} answers" if up else f"no answer at {URL}",
                    "" if up else ".venv/bin/python scripts/heading-install.py"))
    watch, why = agent_watch_state(p.which("claude"), Path.home(), root)
    rows.append(Row("agentview", "agent-watch", OK if watch else RED, why[:160],
                    "" if watch else ".venv/bin/python scripts/heading-install.py"))

    # Mail: the last job's outcome from the daemon's own log (HOS-294). With
    # Google mail the Exchange rows do not apply; check_google reports instead.
    if _mail_is_google(p.engine_root):
        return rows
    if _mail_by_choice_off(p.engine_root):
        rows.append(Row("mail", "account", OK, "not connected, by choice at install"))
    elif not _setting(env, "EXCHANGE_EMAIL"):
        rows.append(Row("mail", "account", RED, "no mail account configured",
                        _remedy(root, "set EXCHANGE_EMAIL, EXCHANGE_PASSWORD, EXCHANGE_SERVER")))
    else:
        absent = [n for n in EXCHANGE_SETTINGS if not _setting(env, n)]
        if absent:
            rows.append(Row("mail", "account", RED, f"not set: {', '.join(absent)}",
                            f"set {', '.join(absent)} in .env"))
        unit = p.unit("sync-exchange-daemon.service") if p.platform.startswith("linux") else None
        try:
            sig = p.exchange(bool(unit) and unit.get("LoadState") == "loaded")
        except (OSError, ValueError, RuntimeError) as exc:
            rows.append(Row("mail", "last sync", RED, f"not read: {exc}"[:160],
                            "bash scripts/install-daemon-service.sh sync-exchange"))
        else:
            sev = sig.get("severity", "ok")
            rows.append(Row("mail", "last sync",
                            RED if sev in ("high", "critical") else WARN if sev == "warn" else OK,
                            str(sig.get("summary", ""))[:160],
                            "" if sev == "ok" else "journalctl --user -u sync-exchange-daemon.service -n 50"))
    return rows


def check_plugins(p: Probes) -> list[Row]:
    try:
        settings = json.loads((p.engine_root / ".claude" / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [Row("plugins", "enabledPlugins", RED, f".claude/settings.json unreadable: {exc}"[:160],
                    _remedy(p.engine_root, "clone the engine again"))]
    wanted = sorted(k for k, v in (settings.get("enabledPlugins") or {}).items() if v is True)
    wanted += [k for k in MODULE_PLUGINS if k not in wanted]
    installed = p.installed_plugins()
    if installed is None:
        return [Row("plugins", "installed", RED, "no installed_plugins.json",
                    _remedy(p.engine_root, f"claude plugin install {wanted[0]}" if wanted else "-"))]
    missing = [k for k in wanted if k not in installed]
    return [Row("plugins", "installed", RED if missing else OK,
                f"missing: {', '.join(missing)}" if missing else f"{len(wanted)} of {len(wanted)}",
                "" if not missing else _remedy(p.engine_root, f"claude plugin install {missing[0]}"))]


def check_home_tools(p: Probes) -> list[Row]:
    """Every hook and MCP command naming a tool under the home folder by absolute path (HOS-312, F5)."""
    from scripts.utils.install_commands import bare_home_tools, home_tools

    home = Path.home()
    found = bare_home_tools(home, home_tools(home, p.engine_root))
    if not found:
        return [Row("install", "home tools", OK, "every home tool named by absolute path")]
    return [Row("install", "home tools", RED, line.replace(str(home), "~")[:160], "run the installer again")
            for line in found]


def check_fleet(p: Probes) -> list[Row]:
    rows = []
    try:
        beats = p.beats()
    except (OSError, ValueError, RuntimeError, AttributeError) as exc:
        return [Row("daemons", "heartbeats", RED, f"not read: {exc}"[:160], "")]
    bad = [(d, s, why) for d, s, why in beats if s != "ok"]
    rows.append(Row("daemons", "heartbeats", RED if bad else OK if beats else WARN,
                    "; ".join(f"{d} {s}" + (f" ({why.splitlines()[0][:60]})" if why else "")
                              for d, s, why in bad)[:160] if bad else
                    f"{len(beats)} beating" if beats else "no heartbeat written yet",
                    "" if not bad else ".venv/bin/python scripts/daemon-fleet-health.py"))
    if p.platform.startswith("linux"):
        loops = p.restart_loops()
        rows.append(Row("daemons", "restart loops", RED if loops.get("due") else OK,
                        str(loops.get("summary", ""))[:160],
                        "journalctl --user -u <unit> -n 50" if loops.get("due") else ""))
    return rows


def check_admin_console(p: Probes) -> list[Row]:
    """The admin console page is built and newer than the list of web interfaces (HOS-333)."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from scripts.utils.data_path import DataPathError, data_path

    if p.data_root is None:
        return [Row("install", "admin console", WARN, "no data repository here, so no console page", "")]
    try:
        page = data_path("runtime:admin-console/cache", data_root=p.data_root) / "index.html"
    except DataPathError:
        return [Row("install", "admin console", WARN, "this data repository's registry has no admin-console row",
                    "add the admin-console row to this data repository's runtime services registry")]
    build = ".venv/bin/python scripts/heading-console.py build"
    try:
        built = page.stat().st_mtime
    except OSError:
        return [Row("install", "admin console", RED, "not built", build)]
    registry = p.engine_root / "scripts" / "utils" / "web_interfaces.py"
    try:
        newer = built > registry.stat().st_mtime
    except OSError:
        newer = True  # no list to be older than
    if not newer:
        return [Row("install", "admin console", WARN, "older than the list of web interfaces", build)]
    zone = install_answers.timezone(p.engine_root)
    when = datetime.fromtimestamp(built, ZoneInfo(zone) if zone else timezone.utc)
    return [Row("install", "admin console", OK, f"built {when:%Y-%m-%d %H:%M}")]


def run_checks(p: Probes) -> list[Row]:
    env = _settings(p.engine_root)
    rows = check_engine(p) + check_data(p) + check_hooks(p) + check_secrets(p, env)
    rows += check_modules(p, env) + check_google(p, env) + check_units(p, env) + check_plugins(p) + check_home_tools(p)
    rows += check_fleet(p) + check_admin_console(p)
    return rows


def render(rows: list[Row]) -> str:
    w_c = max(len("COMPONENT"), *(len(r.component) for r in rows))
    w_k = max(len("CHECK"), *(len(r.check) for r in rows))
    lines = [f"{'STATUS':<6}  {'COMPONENT':<{w_c}}  {'CHECK':<{w_k}}  DETAIL",
             f"{'-' * 6}  {'-' * w_c}  {'-' * w_k}  {'-' * 6}"]
    for r in rows:
        lines.append(f"{r.status.upper():<6}  {r.component:<{w_c}}  {r.check:<{w_k}}  {r.detail}")
        if r.status == RED and r.remedy:
            lines.append(f"{'':<6}  {'':<{w_c}}  {'':<{w_k}}  fix: {r.remedy}")
    red = sum(r.status == RED for r in rows)
    warn = sum(r.status == WARN for r in rows)
    lines.append("")
    lines.append(f"{len(rows)} checks: {red} red, {warn} warn, "
                 f"{len(rows) - red - warn} ok. " + ("All green." if not red else "Not ready."))
    return "\n".join(lines)


def default_probes() -> Probes:
    from scripts.utils.paths import DataRootError, data_root_is_demo, get_data_root
    from scripts.utils.workspace import get_workspace_root
    try:
        data = None if data_root_is_demo() else get_data_root()
    except DataRootError as exc:
        print(f"doctor: data root not resolved: {exc}", file=sys.stderr)
        data = None
    return Probes(engine_root=get_workspace_root(), data_root=data)


def main(argv: list[str] | None = None) -> int:
    from scripts.utils.clone_guard import require_main_clone
    require_main_clone(__file__)
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", help="machine-readable rows")
    args = ap.parse_args(argv)
    rows = run_checks(default_probes())
    if args.json:
        print(json.dumps({"ok": not any(r.status == RED for r in rows),
                          "rows": [asdict(r) for r in rows]}, indent=2, ensure_ascii=False))
    else:
        print(render(rows))
    return 1 if any(r.status == RED for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
