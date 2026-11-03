#!/usr/bin/env python3
"""Scheduled task/agent install: Sentinel's schedule, and the service layer for every unit family.

Centralizes the launchd (macOS) and systemd-user (Linux) logic. Two callers:
`scripts/setup.py` (Sentinel's 15-minute check) and, since HOS-303
(2026-11-01), `scripts/heading-services.py`, which drives the service layer at
the end of this module: one manifest of every unit family in
`scripts/templates/systemd/`, installed as systemd user units on Linux and as
launchd agents on macOS.

The workspace-sync schedule was retired in 2026-06: the destructive
orphan-delete engine is gone, code-down is a plain `git pull`, data-up is
`push-all.py`, and first-run record recovery is `import-legacy-records.py`.

Until 2026-09-25 this module also carried an uninstall API
(`uninstall_sync_schedule`, `uninstall_sentinel_schedule` and a private
teardown per platform) for the executive offboarding and revocation tools.
Those tools were removed with the executive model that day (HOS-130), which
left the uninstallers with no caller, so they went too.

Public API:
    install_sentinel_schedule(slug, workspace_dir, target_platform=None)
    The service layer: SERVICE_FAMILIES, select_families, manifest_problems,
    manifest_rows, install_services, remove_services, service_status,
    render_services (see its section banner below).

Returns True on verified success, False otherwise. A Windows host is not a
target: it gets the unsupported-platform notice; run the workspace under WSL2.
"""

import functools
import getpass
import os
import platform as platform_mod
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.utils.colors import GREEN, RED, RESET, YELLOW


def _ok(msg: str) -> None:
    print(f"  {GREEN}[ok]{RESET} {msg}")


def _warn(msg: str) -> None:
    print(f"  {YELLOW}[warn]{RESET} {msg}")


def _fail(msg: str) -> None:
    print(f"  {RED}[fail]{RESET} {msg}")


def _run(cmd, check: bool = False) -> subprocess.CompletedProcess:
    """Run a subprocess command. Always captures output, never raises.

    A command that is not on PATH raises FileNotFoundError out of
    `subprocess.run` before any return value exists (measured 2026-08-26).
    Callers all read `.returncode`, so the missing binary is reported the way an
    ordinary failure is: exit 127, the shell's own code for "command not found".
    """
    try:
        return subprocess.run(cmd, check=check, capture_output=True, text=True)
    except (OSError, IndexError) as exc:
        # IndexError is what an empty command list produces inside subprocess,
        # and the docstring's promise has to cover that too.
        return subprocess.CompletedProcess(
            cmd, 127, stdout="",
            stderr=f"{cmd[0] if cmd else '(empty command)'}: {exc}")


def _resolve_python() -> str | None:
    """Return absolute path to a working Python 3.11+ interpreter.

    Prefers sys.executable (the interpreter running us) since that is already
    verified. Falls back to shutil.which on candidate names, rejecting the
    Microsoft Store stub on Windows.
    """
    if sys.executable and Path(sys.executable).exists():
        return sys.executable
    for candidate in ("python3", "python", "py"):
        path = shutil.which(candidate)
        if not path:
            continue
        if "WindowsApps" in path:
            continue
        return path
    return None


def _resolve_python3_mac() -> str:
    """Resolve an absolute path to python3 for a macOS launchd plist.

    launchd agents do NOT inherit the user's shell PATH. Homebrew on Apple
    Silicon installs to /opt/homebrew/bin/python3; Intel Macs use
    /usr/local/bin/python3; system Python 3 lives at /usr/bin/python3 only
    when Xcode Command Line Tools are installed.
    """
    path = shutil.which("python3")
    if path and Path(path).exists():
        return path
    for fallback in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"):
        if Path(fallback).exists():
            return fallback
    # Last-resort: return the bare name and let launchd fail with a useful error.
    return "/usr/local/bin/python3"


def _ensure_logs_dir(workspace_dir: Path) -> Path:
    """The directory the installed unit's output is appended to, created.

    It is the data repository's `runtime:sentinel/logs`. It was
    `<workspace>/.sync/logs/` for every workspace until 2026-09-25, when HOS-5
    section 7 took every artifact out of the engine checkout (operator decision
    that day: "old way should go out and do not stay in the workspace"). A
    second branch kept `.sync/logs/` for a workspace other than this checkout,
    an executive's; it went with the executive model the same day (HOS-130).
    Resolved at install time: the path is baked into the unit.
    """
    from scripts.utils.data_path import data_write_dir

    return data_write_dir("runtime:sentinel/logs")


# =============================================================================
# macOS - launchd
# =============================================================================


def _install_launchd_agent(
    *,
    label: str,
    workspace_dir: Path,
    script_rel_path: str,
    script_args: list,
    start_interval: int,
) -> bool:
    """Install a macOS launchd user agent with verification."""
    logs_dir = _ensure_logs_dir(workspace_dir)

    python3_path = _resolve_python3_mac()
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    script_abs = workspace_dir / script_rel_path
    log_stem = label.replace(".", "-")

    args_xml = ""
    if script_args:
        args_xml = "\n        " + "\n        ".join(
            f"<string>{a}</string>" for a in script_args
        )

    plist_content = textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>{label}</string>
            <key>ProgramArguments</key>
            <array>
                <string>{python3_path}</string>
                <string>{script_abs}</string>{args_xml}
            </array>
            <key>WorkingDirectory</key>
            <string>{workspace_dir}</string>
            <key>StartInterval</key>
            <integer>{start_interval}</integer>
            <key>StandardOutPath</key>
            <string>{logs_dir}/{log_stem}-stdout.log</string>
            <key>StandardErrorPath</key>
            <string>{logs_dir}/{log_stem}-stderr.log</string>
            <key>ProcessType</key>
            <string>Background</string>
            <key>LowPriorityIO</key>
            <true/>
            <key>Nice</key>
            <integer>5</integer>
        </dict>
        </plist>
    """)

    plist_path.write_text(plist_content, encoding="utf-8")

    if _load_launchd_plist(label, plist_path):
        _ok(f"macOS launchd: {label} (every {start_interval}s) - loaded and verified")
        return True
    return False


def _load_launchd_plist(label: str, plist_path: Path) -> bool:
    """(Re)load one user agent and confirm launchd knows it. Shared by Sentinel and the service layer."""
    # Idempotency: unload any existing instance before reloading.
    _run(["launchctl", "unload", str(plist_path)])

    uid = os.getuid() if hasattr(os, "getuid") else None
    loaded = False
    if uid is not None:
        result = _run(["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)])
        loaded = result.returncode == 0
    if not loaded:
        result = _run(["launchctl", "load", str(plist_path)])
        loaded = result.returncode == 0

    if not loaded:
        _fail(f"launchctl could not load {label}. Plist at: {plist_path}")
        return False

    verify = _run(["launchctl", "list", label])
    if verify.returncode == 0:
        return True
    _warn(f"launchctl loaded but 'list' cannot find {label}. Agent may still run.")
    return False


# =============================================================================
# Linux - systemd user timer
# =============================================================================


def _install_systemd_user_timer(
    *,
    unit_name: str,
    workspace_dir: Path,
    script_rel_path: str,
    script_args: list,
    cadence_min: int,
    description: str,
) -> bool:
    """Install a systemd user .service + .timer pair under `systemd_user_dir()`.

    Idempotent: re-enables existing unit. Returns True on verified success.
    Writes logs to `_ensure_logs_dir(workspace_dir)/<unit_name>.log`.
    """
    py = _resolve_python()
    if not py:
        _fail(f"Cannot install timer {unit_name}: no Python 3 interpreter found.")
        return False

    if not shutil.which("systemctl"):
        _fail("systemctl not found. systemd user units require systemd >= 226.")
        return False

    user_units_dir = systemd_user_dir()
    user_units_dir.mkdir(parents=True, exist_ok=True)

    log_path = _ensure_logs_dir(workspace_dir) / f"{unit_name}.log"

    args_str = " ".join(script_args) if script_args else ""
    script_abs = workspace_dir / script_rel_path
    exec_start = f'{py} {script_abs}'
    if args_str:
        exec_start = f'{exec_start} {args_str}'

    service_unit = user_units_dir / f"{unit_name}.service"
    service_content = textwrap.dedent(f"""\
        [Unit]
        Description={description}
        After=network.target

        [Service]
        Type=oneshot
        WorkingDirectory={workspace_dir}
        ExecStart={exec_start}
        StandardOutput=append:{log_path}
        StandardError=append:{log_path}
        Environment=PYTHONUNBUFFERED=1
    """)
    service_unit.write_text(service_content, encoding="utf-8")

    timer_unit = user_units_dir / f"{unit_name}.timer"
    timer_content = textwrap.dedent(f"""\
        [Unit]
        Description=Timer for {description}

        [Timer]
        OnBootSec=2min
        OnUnitActiveSec={cadence_min}min
        Persistent=true
        Unit={unit_name}.service

        [Install]
        WantedBy=timers.target
    """)
    timer_unit.write_text(timer_content, encoding="utf-8")

    _run(["systemctl", "--user", "daemon-reload"])
    result = _run(["systemctl", "--user", "enable", "--now", f"{unit_name}.timer"])
    if result.returncode != 0:
        _fail(f"systemctl enable failed: {result.stderr.strip() or result.stdout.strip()}")
        return False

    verify = _run(["systemctl", "--user", "is-active", f"{unit_name}.timer"])
    if verify.returncode != 0:
        # The success line below used to print anyway, one line under this
        # warning, so an install that verified INACTIVE still ended in a green
        # "enabled" and returned True. `enable --now` is meant to leave the
        # timer running; if it is not running, nothing fires until the next
        # boot, and the installer's own check is what said so. Reproduced
        # 2026-08-26 with is-active stubbed to report "inactive".
        _fail(f"timer enabled but is-active reports: "
              f"{verify.stdout.strip() or verify.stderr.strip() or 'no output'}; "
              f"it will not fire until this is resolved")
        return False

    if not _ensure_linger():
        return False

    _ok(f"systemd user timer: {unit_name}.timer (every {cadence_min}m) — enabled and active")
    return True


def _linger_enabled(user: str) -> bool:
    result = _run(["loginctl", "show-user", user, "-p", "Linger", "--value"])
    return result.returncode == 0 and result.stdout.strip() == "yes"


def _ensure_linger() -> bool:
    """Enable linger for the current user, so the timer fires after an unattended reboot.

    Without it a user timer stays silent until somebody logs in; the installer
    used to print a hint and return True anyway. Checked after the attempt, and
    a failure is reported with the command to run.
    """
    user = getpass.getuser()
    if _linger_enabled(user):
        return True
    _run(["loginctl", "enable-linger", user])
    if _linger_enabled(user):
        _ok(f"loginctl: linger enabled for {user}")
        return True
    _fail(f"linger is not enabled for {user}: the timer will not run after a "
          f"reboot until someone logs in. Run: loginctl enable-linger {user}")
    return False


# =============================================================================
# Public API
# =============================================================================


def install_sentinel_schedule(
    slug: str,
    workspace_dir: Path,
    target_platform: str = None,
) -> bool:
    """Install the 15-minute Sentinel check scheduled task for this workspace."""
    target_platform = (target_platform or platform_mod.system()).lower()
    sentinel_script = workspace_dir / "scripts" / "sentinel.py"
    if not sentinel_script.exists():
        _warn(f"sentinel.py not found at {sentinel_script}; skipping Sentinel scheduling.")
        return False
    if target_platform == "darwin":
        return _install_launchd_agent(
            label=f"io.headingos.sentinel.{slug}",
            workspace_dir=workspace_dir,
            script_rel_path="scripts/sentinel.py",
            script_args=["--check"],
            start_interval=900,
        )
    if target_platform == "linux":
        return _install_systemd_user_timer(
            unit_name=f"heading-sentinel-{slug}",
            workspace_dir=workspace_dir,
            script_rel_path="scripts/sentinel.py",
            script_args=["--check"],
            cadence_min=15,
            description=f"Sentinel comms monitor ({slug})",
        )
    _warn(f"Unsupported platform: {target_platform}. Add manually to crontab:")
    print(f"         */15 * * * * cd {workspace_dir} && python3 scripts/sentinel.py --check")
    return False


# =============================================================================
# The service layer (HOS-303): every unit family, one manifest, two platforms
# =============================================================================
#
# The manifest names each family once: its template files, whether a fresh
# install turns it on, and why not when it does not. Schedule and command are
# READ from the templates rather than written here a second time, so a schedule
# still changes in one place. On Linux a family that has an installer is
# installed BY that installer, which keeps its own pre-checks (interpreter
# imports, timezone-suffixed OnCalendar support, its extra placeholders);
# rendering goes through the same `render_unit` of `scripts/lib/render-units.sh`
# the installers source. The layer writes units itself only where no installer
# exists, and on macOS, where none ever did (deployment design section 2 step 7,
# approved 2026-11-01). macOS is designed in and UNTESTED on a Mac.

TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "scripts" / "templates" / "systemd"
LAUNCHD_PREFIX = "com.headingos."
_TOKEN_RE = re.compile(r"\{\{([A-Z_]+)\}\}")
_BASE_TOKENS = {"WORKSPACE", "PYTHON", "TZ"}
# A value lands in a sed expression and then in a unit line, where these break
# one or the other (the same set `install-nightly-refresh-timer.sh` refuses).
_UNSAFE_VALUE_RE = re.compile(r'[|&\\"%\n\r]')
# The one command that leaves /etc/wsl.conf with exactly one [boot] section
# holding systemd=true: every [boot] section merged (the first value of a
# repeated key wins), every other section and line kept, the file's mode kept.
# Only sudo and python3, both on a fresh WSL Ubuntu. An optional argument names
# another file, so the same command can be shown on a copy. Lines are read the
# way heading-install's ConfigParser() reads them: a full-line comment starts
# with # or ;, there are no inline comments, a line indented deeper than the
# option above it continues that option, and a header is ConfigParser's own
# SECTCRE matched on the stripped line, so `[boot] # startup` is [boot].
_WSL_SYSTEMD_PY = r'''
import configparser,os,re,sys
p=(sys.argv[1:] or ["/etc/wsl.conf"])[0]
t=open(p).read() if os.path.exists(p) else ""
H=configparser.ConfigParser.SECTCRE
out,notes,keys=[],[],{}
sect,opt,ind,last=None,False,0,None
for l in t.splitlines():
    v=l.strip()
    n=len(l)-len(l.lstrip())
    if v and v[0] not in "#;" and not (opt and n>ind):
        ind=n
        m=H.match(v)
        if m:
            sect,opt,last=m.group("header"),False,None
            if sect=="boot":
                continue
        else:
            opt=True
            if sect=="boot":
                k=re.split("[=:]",v,1)[0].strip().lower()
                last=None if k in keys or k=="systemd" else k
                if last:
                    keys[k]=[l]
                continue
    elif sect=="boot":
        if v and v[0] in "#;":
            notes.append(l)
        elif v and last:
            keys[last].append(l)
        continue
    out.append(l)
o="\n".join(out)
o+=("\n" if o else "")+"\n".join(["[boot]"]+notes+[x for k in keys.values() for x in k]+["systemd=true"])+"\n"
m=os.stat(p).st_mode&0o7777 if os.path.exists(p) else 0o644
f=open(p+".tmp","w")
f.write(o)
f.close()
os.chmod(p+".tmp",m)
os.replace(p+".tmp",p)
'''
WSL_SYSTEMD_FIX = f"sudo python3 -c '{_WSL_SYSTEMD_PY}'"


class ServiceLayerError(RuntimeError):
    """A refusal the caller prints as one plain sentence."""


@dataclass(frozen=True)
class ServiceFamily:
    name: str
    units: tuple[str, ...]
    mandatory: bool
    why: str = ""                              # why a fresh install leaves it off
    installer: tuple[str, ...] | None = None   # Linux: argv under the workspace
    platforms: tuple[str, ...] = ("linux", "darwin")
    this_machine: bool = True                  # False: the unit belongs on another host
    adopts_foreign: bool = True                # a unit file of that name not written here is left alone
    conflicts_with: tuple[str, ...] = ()       # units whose presence here means this family stays off

    @property
    def kind(self) -> str:
        return "timer" if any(u.endswith(".timer") for u in self.units) else "service"

    @property
    def enable_units(self) -> tuple[str, ...]:
        """The units `systemctl --user enable` is given: the timers, else the service."""
        timers = tuple(u for u in self.units if u.endswith(".timer"))
        return timers or tuple(u for u in self.units if u.endswith(".service"))


def _pair(stem: str) -> tuple[str, str]:
    return (f"{stem}.service", f"{stem}.timer")


def _timer_family(stem: str, mandatory: bool = True, why: str = "", installer: str | None = None,
                  **kw) -> ServiceFamily:
    script = installer or f"install-{stem}-timer.sh"
    return ServiceFamily(stem, _pair(stem), mandatory, why, (f"scripts/{script}",), **kw)


def _daemon_family(stem: str, target: str, why: str) -> ServiceFamily:
    return ServiceFamily(stem, (f"{stem}.service",), False, why,
                         ("scripts/install-daemon-service.sh", target))


_MAIL_STEP = "turned on by the mail and calendar step once the executive has chosen (design section 5 item 9)"

# Which families a fresh install turns on is HELM's proposal under the approved
# design ("every module mandatory"), 2026-11-01: on unless the family serves one
# operator's own infrastructure, needs a later install step first, or is retired.
SERVICE_FAMILIES: tuple[ServiceFamily, ...] = (
    # HOS-308: AgentsView's server on 127.0.0.1:8080, the versioned binary the
    # installer's agentsview step put in place (scripts/utils/install_agentview.py).
    # An agentsview.service not written here is adopted, never replaced or removed.
    ServiceFamily("agentsview", ("agentsview.service",), True, adopts_foreign=True),
    _timer_family("archive-transcripts"),
    _timer_family("backup-pull", False, "pulls a backup repository from the operator's own database server; a fresh install has none"),
    _timer_family("chronicle"),
    _timer_family("council-models-check", installer="install-council-models-timer.sh"),
    _timer_family("daemon-watchdog"),
    _timer_family("datastore-map"),
    _timer_family("dream-shadow"),
    # `ollama serve` for the local index, installed by the layer itself (no
    # installer script) from the binary the HOS-305 install step put in place.
    ServiceFamily("embedding-server", ("embedding-server.service",), True),
    ServiceFamily("hindsight-sync", _pair("hindsight-sync") + _pair("hindsight-capture-check"), True,
                  installer=("scripts/install-hindsight-sync-timer.sh",)),
    _timer_family("hindsight-health", False,
                  "checks a Hindsight deployment on another machine; an executive install runs Hindsight on this one"),
    ServiceFamily("hindsight-pulse", _pair("hindsight-pulse"), False,
                  "runs on the machine hosting the Hindsight server, as a system unit calling "
                  "/usr/local/sbin/hindsight-status; never a user unit here",
                  this_machine=False),
    _timer_family("hindsight-snapshot-offsite", False,
                  "needs its encrypted Google Drive target and passphrase file set up first"),
    _timer_family("hindsight-snapshot"),
    ServiceFamily("hindsight-tunnel", ("hindsight-tunnel.service",), False,
                  "only when Hindsight runs on another machine reached over ssh (needs "
                  "HINDSIGHT_TUNNEL_ALIAS); the install puts Hindsight on this machine"),
    _timer_family("inference-guard"),
    # HOS-306: the issue tracker's server, `lific start` on the binary and config the
    # installer's lific steps put in place; written by this layer, never by `lific init`.
    ServiceFamily("lific", ("lific.service",), True),
    _timer_family("llm-fit-report", False, "reads Langfuse traces, and the install sets up no Langfuse"),
    _timer_family("memory-hygiene"),
    _timer_family("memory-index-refresh", installer="install-memory-index-timer.sh"),
    _timer_family("nightly-refresh", False, "runs the engine's full test suite every night; a development machine's job"),
    _timer_family("odin-cadence", False, "retired: ops-radar carries its signal, and its installer leaves the timer off"),
    _timer_family("odin-propose"),
    _timer_family("ops-radar"),
    _timer_family("proxy-usage-dashboard", installer="install-proxy-usage-timer.sh"),
    _timer_family("recall-calibration"),
    _timer_family("reminders"),
    _timer_family("router-accuracy"),
    _daemon_family("sentinel", "sentinel", f"the comms monitor; {_MAIL_STEP}"),
    _daemon_family("sync-exchange-daemon", "sync-exchange", f"the Exchange sync; {_MAIL_STEP}"),
    # Linux only: the component updater is "Linux and WSL2 on x86-64 only" (CLIPROXY guide).
    _timer_family("update-manager", platforms=("linux",)),
    # HOS-307: the model proxy, installed by the service layer itself. A
    # cliproxyapi.service already present and not written here is adopted, never
    # replaced or removed: the guide had executives write that unit by hand.
    ServiceFamily("cliproxyapi", ("cliproxyapi.service",), True, adopts_foreign=True),
    # HOS-304: the Hindsight memory server on this machine, installed by the service
    # layer itself from the uv environment the installer's hindsight steps create.
    # Never beside hindsight-tunnel: that unit means Hindsight runs on another
    # machine and 127.0.0.1:8888 is already its forward.
    # Linux only (HOS-304 review round 1): the launchd translation does not carry
    # EnvironmentFile, so on a Mac the server would start without its key.
    ServiceFamily("hindsight-server", ("hindsight-server.service",), True, adopts_foreign=True,
                  platforms=("linux",), conflicts_with=("hindsight-tunnel.service",)),
    # HOS-312: the Hindsight web console beside the server, from the package the
    # installer's hindsight-console step unpacks; Linux only and never beside
    # hindsight-tunnel, for the server's reasons above.
    ServiceFamily("hindsight-control-plane", ("hindsight-control-plane.service",), True, adopts_foreign=True,
                  platforms=("linux",), conflicts_with=("hindsight-tunnel.service",)),
)

#: The first line of every unit this layer renders (a plist carries it as an
#: XML comment straight after the XML declaration, `_plist_bytes`). An existing
#: file without it was written by someone else and is adopted: never written
#: over, enabled, started, stopped, disabled or deleted; `service_status` still
#: reads it. scripts/utils/install_lific.py and install_herdr_cliproxy.py ask
#: `written_by_service_layer` too.
SERVICE_LAYER_MARKER = "# heading-os-service-layer"
#: The first line lific.service carried before HOS-317: a unit an earlier
#: install wrote with it is still the service layer's own.
_EARLIER_MARKER = "# Written by the HEADING OS service layer"


def written_by_service_layer(path: Path) -> bool:
    """True when `path` carries the service layer's marker; an unreadable file is not ours."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[:2]
    except (OSError, UnicodeDecodeError):
        return False
    if path.suffix == ".plist":
        return len(lines) == 2 and lines[0].startswith("<?xml") and lines[1] == f"<!-- {SERVICE_LAYER_MARKER} -->"
    return bool(lines) and lines[0].startswith((SERVICE_LAYER_MARKER, _EARLIER_MARKER))


def _adopted(paths: list[Path]) -> list[Path]:
    """The files among `paths` that exist and were not written by this layer; each one is printed."""
    found = [p for p in paths if p.exists() and not written_by_service_layer(p)]
    for path in found:
        print(f"  adopted: {path} was not written by HEADING OS and is left as it is")
    return found


def _adopted_units(fam: ServiceFamily, dest: Path) -> list[Path]:
    """The systemd units of `fam` in `dest` this layer did not write, printed; adopted, so left alone.

    A family with an installer script is not asked: that script writes its
    units, which have never carried the marker, so every one of them would
    read as foreign and install could no longer update it nor remove delete it.
    """
    if not fam.adopts_foreign or fam.installer:
        return []
    return _adopted([dest / u for u in fam.units])


def select_families(names: list[str] | None = None, *, mandatory_only: bool = False) -> list[ServiceFamily]:
    """The families named (in manifest order), or every one; `mandatory_only` keeps those a fresh install turns on."""
    by_name = {f.name: f for f in SERVICE_FAMILIES}
    if names:
        unknown = [n for n in names if n not in by_name]
        if unknown:
            raise ServiceLayerError(f"no service family is called {', '.join(unknown)}; "
                                    f"known: {', '.join(by_name)}")
        chosen = [f for f in SERVICE_FAMILIES if f.name in names]
    else:
        chosen = list(SERVICE_FAMILIES)
    return [f for f in chosen if f.mandatory] if mandatory_only else chosen


def manifest_problems(workspace_dir: Path | None = None) -> list[str]:
    """Every way the manifest and the template directory disagree; empty when they agree.

    A template no family claims would be shipped and never installed, the gap
    the 2026-11-01 audit found for two families; so every command refuses
    while this list is not empty.
    """
    problems = []
    on_disk = {p.name for p in TEMPLATES_DIR.iterdir() if p.suffix in (".service", ".timer")}
    claimed: dict[str, str] = {}
    for fam in SERVICE_FAMILIES:
        for unit in fam.units:
            if unit in claimed:
                problems.append(f"{unit} is claimed by both {claimed[unit]} and {fam.name}")
            claimed[unit] = fam.name
            if unit not in on_disk:
                problems.append(f"{fam.name}: template {unit} is not in {TEMPLATES_DIR}")
        if fam.installer and workspace_dir is not None and not (workspace_dir / fam.installer[0]).is_file():
            problems.append(f"{fam.name}: installer {fam.installer[0]} does not exist")
    for unit in sorted(on_disk - set(claimed)):
        problems.append(f"template {unit} belongs to no family in SERVICE_FAMILIES")
    return problems


def _directives(text: str) -> dict[str, list[str]]:
    """`Key=Value` lines of a unit file, every value per key, sections ignored (no key repeats across ours)."""
    out: dict[str, list[str]] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";", "[")):
            continue
        key, sep, value = stripped.partition("=")
        if sep:
            out.setdefault(key.strip(), []).append(value.strip())
    return out


def _template(unit: str) -> str:
    return (TEMPLATES_DIR / unit).read_text(encoding="utf-8")


def _service_of(primary: str, unit_text: str) -> str:
    """The .service a primary unit runs: itself, or the timer's `Unit=`, else the same stem."""
    if primary.endswith(".service"):
        return primary
    return (_directives(unit_text).get("Unit") or [primary[: -len(".timer")] + ".service"])[0]


def manifest_rows() -> list[dict]:
    """The manifest as a reader sees it: schedule and command read from the templates."""
    rows = []
    for fam in SERVICE_FAMILIES:
        schedules, commands = [], []
        for primary in fam.enable_units:
            d = _directives(_template(primary))
            if primary.endswith(".timer"):
                when = [f"calendar {v}" for v in d.get("OnCalendar", [])]
                when += [f"every {v}" for v in d.get("OnUnitActiveSec", [])]
                schedules.append("; ".join(when).replace(" {{TZ}}", " (configured timezone)"))
            else:
                schedules.append(f"always running (Restart={(d.get('Restart') or ['no'])[0]})")
            service = _service_of(primary, _template(primary))
            commands.extend(_directives(_template(service)).get("ExecStart", []))
        rows.append({"name": fam.name, "kind": fam.kind, "schedule": " | ".join(schedules),
                     "command": " | ".join(commands), "mandatory": fam.mandatory, "why": fam.why,
                     "platforms": list(fam.platforms), "this_machine": fam.this_machine,
                     "units": list(fam.units)})
    return rows


# -- rendering, through the installers' own shell functions -------------------

@functools.lru_cache(maxsize=None)
def _unit_identity(workspace_dir: Path) -> tuple[str, str]:
    """(interpreter, timezone) for the units, from `resolve_python` and `resolve_unit_tz`, once per process."""
    script = ('set -euo pipefail; ws="$1"; '
              'source "$ws/scripts/lib/resolve-python.sh"; source "$ws/scripts/lib/render-units.sh"; '
              'py="$(resolve_python "$ws")"; '
              '[ -n "$py" ] || { echo "no python3 interpreter found for the units" >&2; exit 4; }; '
              'printf "%s\\n" "$py"; resolve_unit_tz "$ws" "$py"')
    result = _run(["bash", "-c", script, "identity", str(workspace_dir)])
    if result.returncode != 0:
        raise ServiceLayerError(f"cannot resolve the units' interpreter and timezone: "
                                f"{result.stderr.strip() or 'bash exited ' + str(result.returncode)}")
    for line in result.stderr.strip().splitlines():
        _warn(line)  # resolve_unit_tz announces a UTC fallback here
    lines = result.stdout.splitlines()
    if len(lines) < 2:
        raise ServiceLayerError(f"cannot resolve the units' interpreter and timezone: got {result.stdout!r}")
    return lines[0], lines[-1]


def _extra_value(placeholder: str) -> str:
    """Placeholders beyond the three base ones, valued the way their installers value them."""
    if placeholder == "STATE":  # install-hindsight-sync-timer.sh: the sync ledger directory, resolved
        from scripts.utils.data_path import data_path

        return str(data_path("runtime:hindsight-sync/state"))
    if placeholder == "LIMIT":
        return os.environ.get("HINDSIGHT_SYNC_LIMIT", "200")
    if placeholder == "MAX_PENDING":
        return os.environ.get("HINDSIGHT_SYNC_MAX_PENDING", "80")
    if placeholder == "TOOLPATH":  # install-nightly-refresh-timer.sh: the installing shell's PATH
        return os.environ.get("TOOL_PATH") or os.environ.get("PATH", "")
    if placeholder == "OLLAMA_BIN":  # the versioned ollama of scripts/utils/install_index.py (HOS-305)
        from scripts.utils.install_index import ollama_binary

        return str(ollama_binary())
    if placeholder in ("LIFIC_BIN", "LIFIC_CONFIG"):  # HOS-306: where scripts/utils/install_lific.py installs them
        from scripts.utils.install_lific import binary_path, config_path

        return str((binary_path if placeholder == "LIFIC_BIN" else config_path)(Path.home()))
    if placeholder == "HINDSIGHT_ENV":  # HOS-304: the server's uv environment, from its installer
        from scripts.utils.install_hindsight import env_path

        return str(env_path(Path.home()))
    if placeholder == "HINDSIGHT_CONSOLE_DIR":  # HOS-312: where scripts/utils/install_hindsight_console.py unpacks it
        from scripts.utils.install_hindsight_console import install_dir

        return str(install_dir(Path.home()))
    if placeholder in ("NODE_BIN", "NODE_BIN_DIR"):  # HOS-312: node_home()/current of heading-install.py, else the one on PATH
        node = Path.home() / ".local" / "share" / "heading-os" / "node" / "current" / "bin" / "node"
        found = str(node.absolute()) if node.is_file() else shutil.which("node")
        if not found:
            raise ServiceLayerError("no Node.js found: neither the installer's own nor one on PATH")
        return found if placeholder == "NODE_BIN" else str(Path(found).parent)
    if placeholder == "AGENTSVIEW_BIN":  # HOS-308: where scripts/utils/install_agentview.py installs it
        from scripts.utils.install_agentview import binary_path as agentsview_binary

        return str(agentsview_binary(Path.home()))
    if placeholder == "ALIAS":
        alias = os.environ.get("HINDSIGHT_TUNNEL_ALIAS", "").strip()
        if not alias:
            raise ServiceLayerError("hindsight-tunnel needs HINDSIGHT_TUNNEL_ALIAS, the ssh alias "
                                    "whose forwards it holds open")
        return alias
    if placeholder.startswith("CLIPROXY_"):  # HOS-307: the proxy's binary and config, from its installer
        from scripts.utils.install_herdr_cliproxy import unit_values

        values = unit_values(Path.home())
        if placeholder in values:
            return values[placeholder]
    raise ServiceLayerError(f"no value is known for the placeholder {{{{{placeholder}}}}}")


def render_unit_text(unit: str, workspace_dir: Path) -> str:
    """One template rendered by `render_unit` of `scripts/lib/render-units.sh`, returned as text."""
    template = TEMPLATES_DIR / unit
    python, tz = _unit_identity(workspace_dir)
    sed: list[str] = []
    for placeholder in sorted(set(_TOKEN_RE.findall(template.read_text(encoding="utf-8"))) - _BASE_TOKENS):
        value = _extra_value(placeholder)
        if _UNSAFE_VALUE_RE.search(value):
            raise ServiceLayerError(f"{unit}: the value for {{{{{placeholder}}}}} carries a character a unit "
                                    f'line cannot hold (| & \\ " % or a line break): {value!r}')
        sed += ["-e", f"s|{{{{{placeholder}}}}}|{value}|g"]
    script = ('set -euo pipefail; ws="$1"; src="$2"; py="$3"; tz="$4"; shift 4; '
              'source "$ws/scripts/lib/render-units.sh"; '
              'render_unit "$src" /dev/stdout "$ws" "$py" "$tz" "$@"')
    result = _run(["bash", "-c", script, "render", str(workspace_dir), str(template), python, tz, *sed])
    if result.returncode != 0:
        raise ServiceLayerError(f"{unit}: render failed: {result.stderr.strip()}")
    left = _TOKEN_RE.findall(result.stdout)
    if left:
        raise ServiceLayerError(f"{unit}: placeholders survived the render: {', '.join(sorted(set(left)))}")
    return result.stdout


def _with_marker(text: str) -> str:
    return text if text.startswith(SERVICE_LAYER_MARKER) else f"{SERVICE_LAYER_MARKER}\n{text}"


def render_family_systemd(fam: ServiceFamily, workspace_dir: Path) -> dict[str, str]:
    return {unit: _with_marker(render_unit_text(unit, workspace_dir)) for unit in fam.units}


def _plist_bytes(job: dict) -> bytes:
    """The plist with the marker as an XML comment straight after the XML declaration,
    the one place before the document a plist parser accepts a line of ours."""
    head, _, rest = plistlib.dumps(job).partition(b"\n")
    return head + f"\n<!-- {SERVICE_LAYER_MARKER} -->\n".encode("utf-8") + rest


# -- systemd to launchd --------------------------------------------------------

_WEEKDAYS = {"Sun": 0, "Mon": 1, "Tue": 2, "Wed": 3, "Thu": 4, "Fri": 5, "Sat": 6}
_DURATION_RE = re.compile(r"^(\d+)\s*(s|sec|m|min|h|hr)?$")
_DURATION_UNIT = {None: 1, "s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hr": 3600}


def _seconds(value: str) -> int:
    match = _DURATION_RE.match(value.strip())
    if not match:
        raise ServiceLayerError(f"launchd: cannot translate the duration {value!r}")
    return int(match.group(1)) * _DURATION_UNIT[match.group(2)]


def _calendar_field(field: str, top: int, expr: str) -> list[int | None]:
    try:
        if field == "*":
            return [None]
        if "/" in field:
            start, step = field.split("/")
            values = list(range(int(start), top + 1, int(step)))
        else:
            values = [int(v) for v in field.split(",")]
    except ValueError as exc:
        raise ServiceLayerError(f"launchd: cannot translate OnCalendar={expr!r}: {exc}") from exc
    if not values or any(v < 0 or v > top for v in values):
        raise ServiceLayerError(f"launchd: OnCalendar={expr!r} has a value outside 0..{top}")
    return values


def _is_zone(word: str) -> bool:
    """True for a trailing OnCalendar word that is a real zone name; never for a time token.

    A slash alone is not a zone: `*:0/15:00` carries one in its minute step.
    """
    if ":" in word:
        return False
    try:
        ZoneInfo(word)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def calendar_to_launchd(expr: str) -> tuple[list[dict], str | None]:
    """The StartCalendarInterval entries for one OnCalendar expression, and the timezone it named.

    Covers the forms the templates use (weekday, `*-*-*`, `H:M[:00]` with `*`,
    lists and steps, `hourly`, `daily`) and refuses anything else by name, so
    a new template form fails at render rather than scheduling something else.
    """
    words = expr.split()
    tz = words.pop() if len(words) > 1 and _is_zone(words[-1]) else None
    if words == ["hourly"]:
        return [{"Minute": 0}], tz
    if words == ["daily"]:
        return [{"Hour": 0, "Minute": 0}], tz
    weekday = _WEEKDAYS[words.pop(0)] if words and words[0] in _WEEKDAYS else None
    if words and words[0] == "*-*-*":
        words.pop(0)
    parts = words[0].split(":") if len(words) == 1 else []
    if len(parts) not in (2, 3) or (len(parts) == 3 and parts[2] not in ("0", "00")):
        raise ServiceLayerError(f"launchd: cannot translate OnCalendar={expr!r}")
    entries = []
    for hour in _calendar_field(parts[0], 23, expr):
        for minute in _calendar_field(parts[1], 59, expr):
            entry = {} if weekday is None else {"Weekday": weekday}
            if hour is not None:
                entry["Hour"] = hour
            if minute is not None:
                entry["Minute"] = minute
            entries.append(entry)
    return entries, tz


def _mac_local_zone() -> str:
    """The Mac's own zone: the IANA name its /etc/localtime symlink points to; launchd fires in it."""
    try:
        target = os.readlink("/etc/localtime")
    except OSError as exc:
        raise ServiceLayerError(f"launchd: cannot read this Mac's own timezone from /etc/localtime: {exc}") from exc
    name = target.partition("zoneinfo/")[2]
    if not _is_zone(name):
        raise ServiceLayerError(f"launchd: /etc/localtime points to {target}, which names no IANA zone")
    return name


def _same_hours(zone: str, local: str, now: datetime | None = None) -> bool:
    """True when `zone` is `local` by name, or keeps its UTC offset at every hour of the next 366 days."""
    if zone == local:
        return True
    a, b = ZoneInfo(zone), ZoneInfo(local)
    start = (now or datetime.now(timezone.utc)).replace(minute=0, second=0, microsecond=0)
    return all((start + timedelta(hours=h)).astimezone(a).utcoffset()
               == (start + timedelta(hours=h)).astimezone(b).utcoffset() for h in range(366 * 24))


def _launchd_logs_dir() -> Path:
    """Where agent output goes: the user's own log folder, as journald holds it on Linux."""
    return Path.home() / "Library" / "Logs" / "HeadingOS"


def _launchd_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _launchd_labels(fam: ServiceFamily) -> list[str]:
    return [LAUNCHD_PREFIX + _service_of(p, _template(p))[: -len(".service")] for p in fam.enable_units]


def launchd_jobs(fam: ServiceFamily, rendered: dict[str, str], home: Path,
                 logs_dir: Path) -> list[tuple[str, dict, list[str]]]:
    """(label, plist dict, notes) per job: one per timer, or one for a long-running service."""
    jobs = []
    for primary in fam.enable_units:
        notes: list[str] = []
        timer = _directives(rendered[primary]) if primary.endswith(".timer") else None
        service_name = _service_of(primary, rendered[primary])
        svc = _directives(rendered[service_name])
        stem = service_name[: -len(".service")]
        label = LAUNCHD_PREFIX + stem
        exec_lines = svc.get("ExecStart", [])
        if len(exec_lines) != 1:
            raise ServiceLayerError(f"{service_name}: launchd needs exactly one ExecStart, found {len(exec_lines)}")
        job: dict = {"Label": label,
                     "ProgramArguments": [a.replace("%h", str(home)) for a in shlex.split(exec_lines[0])]}
        if "WorkingDirectory" in svc:
            job["WorkingDirectory"] = svc["WorkingDirectory"][0].replace("%h", str(home))
        env = {}
        for raw in svc.get("Environment", []):
            for item in shlex.split(raw):
                key, _, value = item.partition("=")
                env[key] = value.replace("%h", str(home))
        if env:
            job["EnvironmentVariables"] = env
        job["StandardOutPath"] = job["StandardErrorPath"] = str(logs_dir / f"{stem}.log")
        if timer is not None:
            job["ProcessType"] = "Background"
            if "OnCalendar" in timer:
                entries: list[dict] = []
                for expr in timer["OnCalendar"]:
                    found, tz = calendar_to_launchd(expr)
                    entries += found
                    # launchd has no zone: it fires in the Mac's own. A timer naming
                    # another zone with other hours is refused, never shifted.
                    if tz:
                        local = _mac_local_zone()
                        if not _same_hours(tz, local):
                            raise ServiceLayerError(
                                f"{fam.name}: {primary} fires at OnCalendar={expr!r} in {tz}, but launchd "
                                f"has no timezone and runs it in this Mac's own zone, {local}, whose hours "
                                f"differ; set HEADING_OS_TZ to {local}, the Mac's own zone, and run this again")
                job["StartCalendarInterval"] = entries if len(entries) > 1 else entries[0]
            elif "OnUnitActiveSec" in timer:
                job["StartInterval"] = _seconds(timer["OnUnitActiveSec"][0])
            else:
                raise ServiceLayerError(f"{primary}: no OnCalendar or OnUnitActiveSec to translate")
            if "OnBootSec" in timer or "OnStartupSec" in timer:
                job["RunAtLoad"] = True
        else:
            restart = (svc.get("Restart") or ["no"])[0]
            job["RunAtLoad"] = True
            job["KeepAlive"] = (True if restart == "always"
                                else {"SuccessfulExit": False} if restart == "on-failure" else False)
            if (svc.get("Type") or [""])[0] == "notify":
                notes.append(f"{label}: Type=notify has no launchd equivalent; it runs without WatchdogSec")
        jobs.append((label, job, notes))
    return jobs


# -- platform, and the refusal on Linux without systemd user sessions ----------

def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def systemd_user_session_problem() -> str | None:
    """None when `systemctl --user` reaches a user manager; otherwise the plain sentence to print."""
    if not shutil.which("systemctl"):
        reason = "systemctl is not installed"
    else:
        result = _run(["systemctl", "--user", "show-environment"])
        if result.returncode == 0:
            return None
        reason = (result.stderr or result.stdout).strip() or f"systemctl --user exited {result.returncode}"
    return (f"This Linux machine has no systemd user session ({reason}), so HEADING OS cannot run its "
            f"background services here. On WSL, run {WSL_SYSTEMD_FIX} "
            "and restart WSL (wsl --shutdown in Windows); on another Linux, use a distribution that runs systemd. Then run this again.")


def _preflight(target: str, dry_run: bool) -> None:
    if target == "linux":
        if not dry_run:
            problem = systemd_user_session_problem()
            if problem:
                raise ServiceLayerError(problem)
    elif target == "darwin":
        if not dry_run and not shutil.which("launchctl"):
            raise ServiceLayerError("launchctl is not on PATH, so no launchd agent can be loaded.")
    else:
        raise ServiceLayerError(f"{target} is not a target: run HEADING OS inside WSL2 on Windows.")


def _target(target_platform: str | None) -> str:
    return (target_platform or platform_mod.system()).lower()


def conflict_reason(fam: ServiceFamily, env: dict[str, str] | None = None) -> str | None:
    """Why `fam` stays off on this machine because a unit it conflicts with is installed, or None.

    Present means the unit file in the user unit directory (`systemd_user_dir(env)`),
    or its launchd agent under that environment's HOME.
    """
    env = os.environ if env is None else env
    home = Path(env["HOME"]) if env.get("HOME") else Path.home()
    for unit in fam.conflicts_with:
        stem = unit.rsplit(".", 1)[0]
        found = [p for p in (systemd_user_dir(env) / unit,
                             home / "Library" / "LaunchAgents" / f"{LAUNCHD_PREFIX}{stem}.plist") if p.is_file()]
        if found:
            return (f"{fam.name}: not installed here, because {found[0]} is: the two must not run on one "
                    f"machine. Remove {unit} first if {fam.name} is wanted here")
    return None


def _skip_reason(fam: ServiceFamily, target: str) -> str | None:
    if target not in fam.platforms:
        return f"{fam.name}: not offered on {target} (only {', '.join(fam.platforms)})"
    if not fam.this_machine:
        return f"{fam.name}: not installed here: {fam.why}"
    return None


# -- the operations ------------------------------------------------------------

def _systemd_install(fam: ServiceFamily, workspace_dir: Path, dry_run: bool) -> bool:
    rendered = render_family_systemd(fam, workspace_dir)  # renders in a dry run too, so it is checked
    dest = systemd_user_dir()
    if _adopted_units(fam, dest):
        return True
    if fam.installer:
        cmd = ["bash", str(workspace_dir / fam.installer[0]), *fam.installer[1:]]
        if dry_run:
            print(f"  would run: {' '.join(shlex.quote(c) for c in cmd)}")
            return True
        try:
            return subprocess.run(cmd, cwd=workspace_dir).returncode == 0
        except OSError as exc:
            _fail(f"{fam.name}: cannot run {cmd[1]}: {exc}")
            return False
    if dry_run:
        for unit in rendered:
            print(f"  would write {dest / unit}")
        print(f"  would run: systemctl --user enable --now {' '.join(fam.enable_units)}")
        return True
    dest.mkdir(parents=True, exist_ok=True)
    for unit, text in rendered.items():
        _write_atomic(dest / unit, text.encode("utf-8"))
    _run(["systemctl", "--user", "daemon-reload"])
    result = _run(["systemctl", "--user", "enable", "--now", *fam.enable_units])
    if result.returncode != 0:
        _fail(f"{fam.name}: systemctl enable failed: {result.stderr.strip() or result.stdout.strip()}")
        return False
    _ok(f"{fam.name}: {', '.join(fam.enable_units)} enabled")
    return True


def _launchd_install(fam: ServiceFamily, workspace_dir: Path, dry_run: bool) -> bool:
    agents, logs = _launchd_agents_dir(), _launchd_logs_dir()
    jobs = launchd_jobs(fam, render_family_systemd(fam, workspace_dir), Path.home(), logs)
    for _, _, notes in jobs:
        for note in notes:
            _warn(note)
    if fam.adopts_foreign:
        jobs = [j for j in jobs if not _adopted([agents / f"{j[0]}.plist"])]
    if dry_run:
        for label, _, _ in jobs:
            print(f"  would write and load {agents / (label + '.plist')}")
        return True
    agents.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    ok = True
    for label, job, _ in jobs:
        plist = agents / f"{label}.plist"
        _write_atomic(plist, _plist_bytes(job))
        loaded = _load_launchd_plist(label, plist)
        if loaded:
            _ok(f"{fam.name}: {label} loaded")
        ok = loaded and ok
    return ok


def install_services(fams: list[ServiceFamily], *, workspace_dir: Path,
                     target_platform: str | None = None, dry_run: bool = False) -> bool:
    """Install each family; True when every one that applies here installed. Refusals raise ServiceLayerError."""
    target = _target(target_platform)
    _preflight(target, dry_run)
    ok, installed = True, 0
    for fam in fams:
        skip = _skip_reason(fam, target) or conflict_reason(fam)
        if skip:
            _warn(skip)
            continue
        print(f"{fam.name} ({fam.kind})")
        done = (_systemd_install if target == "linux" else _launchd_install)(fam, workspace_dir, dry_run)
        installed += done
        ok = done and ok
    if target == "linux" and installed:
        if dry_run:
            print("  would ensure linger, so the units run after an unattended reboot")
        elif not _ensure_linger():
            ok = False
    return ok


def remove_services(fams: list[ServiceFamily], *, target_platform: str | None = None,
                    dry_run: bool = False) -> bool:
    """Stop, disable and delete each family's units or agents; True when nothing is left running."""
    target = _target(target_platform)
    _preflight(target, dry_run)
    ok = True
    for fam in fams:
        if target not in fam.platforms or not fam.this_machine:
            continue
        print(f"{fam.name} ({fam.kind})")
        if target == "linux":
            dest = systemd_user_dir()
            if _adopted_units(fam, dest):
                continue
            if dry_run:
                print(f"  would run: systemctl --user disable --now {' '.join(fam.enable_units)}")
                print(f"  would run: systemctl --user stop "
                      f"{' '.join(u for u in fam.units if u.endswith('.service'))}")
                print(f"  would delete {', '.join(str(dest / u) for u in fam.units)} where present")
                continue
            # Disabling a timer leaves a pass already in flight running, so every
            # service of the family is stopped too, and each unit is confirmed down.
            _run(["systemctl", "--user", "disable", "--now", *fam.enable_units])
            services = [u for u in fam.units if u.endswith(".service")]
            if services:
                _run(["systemctl", "--user", "stop", *services])
            for unit in fam.units:
                (dest / unit).unlink(missing_ok=True)
            _run(["systemctl", "--user", "daemon-reload"])
            still = [u for u in fam.units
                     if _run(["systemctl", "--user", "is-active", u]).returncode == 0]
            if still:
                _fail(f"{fam.name}: still active after removal: {', '.join(still)}")
                ok = False
            else:
                _ok(f"{fam.name}: removed")
        else:
            uid = os.getuid() if hasattr(os, "getuid") else None
            for label in _launchd_labels(fam):
                plist = _launchd_agents_dir() / f"{label}.plist"
                if fam.adopts_foreign and _adopted([plist]):
                    continue
                if dry_run:
                    print(f"  would unload {label} and delete {plist}")
                    continue
                if uid is None or _run(["launchctl", "bootout", f"gui/{uid}/{label}"]).returncode != 0:
                    _run(["launchctl", "unload", str(plist)])
                plist.unlink(missing_ok=True)
                if _run(["launchctl", "list", label]).returncode == 0:
                    _fail(f"{label}: launchd still knows it after removal")
                    ok = False
                else:
                    _ok(f"{fam.name}: {label} removed")
    return ok


def service_status(fams: list[ServiceFamily], *, target_platform: str | None = None) -> bool:
    """Print one line per unit or agent; True when every mandatory family here is installed and running."""
    target = _target(target_platform)
    _preflight(target, dry_run=False)
    healthy = True
    for fam in fams:
        if target not in fam.platforms or not fam.this_machine:
            continue
        if target == "linux":
            dest = systemd_user_dir()
            for unit in fam.enable_units:
                enabled = _run(["systemctl", "--user", "is-enabled", unit]).stdout.strip() or "absent"
                active = _run(["systemctl", "--user", "is-active", unit]).stdout.strip() or "unknown"
                present = (dest / unit).is_file()
                good = present and enabled == "enabled" and active == "active"
                line = (f"{fam.name}: {unit} file={'yes' if present else 'no'} "
                        f"enabled={enabled} active={active}")
                (_ok if good else _warn if not fam.mandatory else _fail)(line)
                healthy = healthy and (good or not fam.mandatory)
        else:
            for label in _launchd_labels(fam):
                present = (_launchd_agents_dir() / f"{label}.plist").is_file()
                loaded = _run(["launchctl", "list", label]).returncode == 0
                good = present and loaded
                line = f"{fam.name}: {label} plist={'yes' if present else 'no'} loaded={'yes' if loaded else 'no'}"
                (_ok if good else _warn if not fam.mandatory else _fail)(line)
                healthy = healthy and (good or not fam.mandatory)
    return healthy


def systemd_user_dir(env: dict[str, str] | None = None) -> Path:
    """Where systemd reads a user's own units: ${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user.

    `env` stands in for this process's environment, so an installer resolves the
    directory its child `heading-services.py` will use (that child's HOME and
    XDG_CONFIG_HOME), never a second guess of its own.
    """
    env = os.environ if env is None else env
    home = Path(env["HOME"]) if env.get("HOME") else Path.home()
    return Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "systemd" / "user"


def _service_manager_dirs() -> list[Path]:
    """Every directory a service manager loads units or agents from, resolved.

    systemd's own answer (`systemd-analyze --user unit-paths`, which reads no
    unit and starts nothing) where it exists, plus the XDG defaults in case it
    does not, plus the launchd agent and daemon directories.
    """
    home = Path.home()
    dirs = [
        systemd_user_dir().parent,
        Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share") / "systemd",
        _launchd_agents_dir(), Path("/Library/LaunchAgents"), Path("/Library/LaunchDaemons"),
        Path("/System/Library/LaunchAgents"), Path("/System/Library/LaunchDaemons"),
    ]
    if os.environ.get("XDG_RUNTIME_DIR"):
        dirs.append(Path(os.environ["XDG_RUNTIME_DIR"]) / "systemd")
    if shutil.which("systemd-analyze"):
        result = _run(["systemd-analyze", "--user", "unit-paths"])
        if result.returncode == 0:
            dirs += [Path(line.strip()) for line in result.stdout.splitlines() if line.strip()]
        else:
            _warn(f"systemd-analyze --user unit-paths failed; refusing the default unit "
                  f"directories only: {result.stderr.strip()}")
    return [d.resolve() for d in dirs]


def render_services(fams: list[ServiceFamily], dest: Path, *, workspace_dir: Path,
                    target_platform: str | None = None) -> list[Path]:
    """Write what install would write into `dest` instead, for inspection; installs and loads nothing.

    Refuses a destination inside the engine checkout or any directory a service
    manager loads units from (`_service_manager_dirs`): a render that landed
    there would be an install.
    """
    target = _target(target_platform)
    dest = dest.expanduser().resolve()
    for forbidden in [workspace_dir.resolve(), *_service_manager_dirs()]:
        if dest == forbidden or forbidden in dest.parents:
            raise ServiceLayerError(f"render refuses {dest}: it is inside {forbidden}, "
                                    f"where a service manager or this checkout reads")
    if target not in ("linux", "darwin"):
        raise ServiceLayerError(f"{target} is not a target: run HEADING OS inside WSL2 on Windows.")
    dest.mkdir(parents=True, exist_ok=True)
    written = []
    for fam in fams:
        rendered = render_family_systemd(fam, workspace_dir)
        if target == "linux":
            for unit, text in rendered.items():
                _write_atomic(dest / unit, text.encode("utf-8"))
                written.append(dest / unit)
            continue
        for label, job, notes in launchd_jobs(fam, rendered, Path.home(), _launchd_logs_dir()):
            for note in notes:
                _warn(note)
            path = dest / f"{label}.plist"
            _write_atomic(path, _plist_bytes(job))
            written.append(path)
    return written
