#!/usr/bin/env python3
"""Chromium-family browser automation helper (CDP-attach pattern).

Default browser for all Claude-driven browser automation: **Brave** (cross-
platform, yt-dlp-native, ClaudeCode profile pre-authenticated). Comet
(Perplexity's Chromium-based browser, Windows + macOS only) is retained as
an opt-in fallback via `--browser comet`; new automation defaults to Brave.

Why CDP-attach pattern (works for Comet AND Brave):
  - `launch_persistent_context(executable_path=...)` fails — Comet closes
    the CDP-controlled tab immediately; some Brave builds do the same.
  - Vivaldi rejects Playwright's launch flags outright (do not use it for
    automation; the operator's auto-memory holds the browser decision).
  - `connect_over_cdp` against an externally-launched browser is stable.

Critical rules:
  - NEVER call `browser.close()` on a CDP-attached session — it kills the
    whole browser. Let the `sync_playwright()` context exit naturally.
  - Comet's Perplexity sidecar injects tabs (`perplexity.ai/sidecar`,
    `perplexity.ai/b/home`). Brave does not have a sidecar but other
    extensions can inject background tabs. Filter tabs by URL substring,
    not `ctx.pages[0]`.
  - The profile display name "ClaudeCode" maps to folder name "Default" in
    both Comet and Brave.

Usage:
    from scripts.browser import launch_browser, attach, pick_tab

    launch_browser("brave", port=9222, initial_url="https://zoom.us/signin")
    with attach(port=9222) as (browser, ctx):
        page = pick_tab(ctx, "zoom.us")
        page.bring_to_front()
        page.goto("https://zoom.us/profile/setting", wait_until="domcontentloaded")
        # ... do work ...

CLI:
    python scripts/browser.py launch --proxy-slot direct --url https://zoom.us/signin
    python scripts/browser.py launch --proxy-slot 1
    python scripts/browser.py routes
    python scripts/browser.py status
    python scripts/browser.py stop

`launch` has no default route (HOS-140, operator request 2026-09-26). Name one
with `--proxy-slot direct|1|2|3` (`0` still means direct). Omitted on a
terminal, the four routes are shown with their exit IP and country, measured
through each route at that moment, and the operator picks one; omitted off a
terminal, `launch` refuses with exit code 7 before anything starts and prints
the same menu. `routes` prints the menu alone and exits 0; it starts no relay
and no browser. The mechanism and why the country is trusted:
`scripts/utils/proxy_route.py`.

`--proxy-slot N` sends the whole browser through the `DECODO_PROXY_N` SOCKS5
slot. A Chromium-family browser cannot authenticate to SOCKS5, so the launch
first starts `scripts/utils/socks_relay.py` as a child process: a loopback
relay that reads the slot from `.env` itself (the password is in no argv),
answers `READY <port>`, and is then handed the browser's PID and exits when
that process is gone. The browser gets `--proxy-server=socks5://127.0.0.1:<port>`
and the WebRTC flags that keep UDP off the direct route. **A launch with a slot
never falls back to no proxy**: no relay, no browser. That refusal is the
point, because the first hand-run attempt on 2026-09-25 launched with an empty
proxy value and so went out direct. `stop` stops the relay after the browser;
`status` checks that the browser's own command line names the relay, that the
relay is up, and asks the exit IP through it and directly.

Tests: none live. The launch-argument allowlist below is unchanged and still
narrow, but the check that a flag outside it is refused went in the 2026-09-21
test-suite split. Widening that list now costs nothing and warns nobody.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Iterator, List, Optional, Tuple
from urllib.request import urlopen

# Allow `from scripts.browser import ...` whether imported by a skill
# script or invoked directly as `python scripts/browser.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.data_path import data_path  # noqa: E402
from scripts.utils.user_agent import user_agent  # noqa: E402
from scripts.utils.data_path import data_write_dir  # noqa: E402
from scripts.utils.data_path import data_write_file  # noqa: E402
from scripts.utils.colors import GREEN, YELLOW, RED, CYAN, GRAY, RESET  # noqa: E402
from scripts.utils.atomic import atomic_write_text  # noqa: E402
from scripts.utils.proxy_route import (  # noqa: E402
    EXIT_ROUTE_NOT_CHOSEN, IP_CHECK_URL, add_route_argument, format_menu, probe_all,
    resolve_route,
)

# Browser configuration table. Per-platform paths for each supported
# Chromium-family browser the workspace knows how to launch with CDP.
# Brave is the default on every platform (see DEFAULT_BROWSER below and the
# module docstring). Comet is a Windows/macOS-only opt-in fallback and has no
# Linux build at all.
_BROWSER_CONFIGS = {
    "comet": {
        "win32": {
            "exe": r"C:\Program Files\Perplexity\Comet\Application\comet.exe",
            "user_data": r"~\AppData\Local\Perplexity\Comet\User Data",
            "process_name": "comet.exe",
        },
        "darwin": {
            "exe": "/Applications/Comet.app/Contents/MacOS/Comet",
            "user_data": "~/Library/Application Support/Perplexity/Comet",
            "process_name": "Comet",
        },
        # Linux: no Comet build exists. Selecting comet on Linux raises.
    },
    "brave": {
        "win32": {
            "exe": r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
            "user_data": r"~\AppData\Local\BraveSoftware\Brave-Browser\User Data",
            "process_name": "brave.exe",
        },
        "darwin": {
            "exe": "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "user_data": "~/Library/Application Support/BraveSoftware/Brave-Browser",
            "process_name": "Brave Browser",
        },
        "linux": {
            "exe": "/usr/bin/brave-browser",  # also /usr/bin/brave on some distros
            "user_data": "~/.config/BraveSoftware/Brave-Browser",
            "process_name": "brave",
        },
    },
}

DEFAULT_BROWSER = "brave"
PROFILE_FOLDER = "Default"  # display name "ClaudeCode" lives in this folder
DEFAULT_PORT = 9222

# `IP_CHECK_URL` lives beside `GEO_CHECK_URL` in `scripts/utils/proxy_route.py`.
# Without these, WebRTC can open UDP straight past a SOCKS proxy and reveal the
# direct address; `disable_non_proxied_udp` keeps it on the proxy or off.
PROXY_WEBRTC_FLAGS = ("--webrtc-ip-handling-policy=disable_non_proxied_udp",
                      "--force-webrtc-ip-handling-policy")
RELAY_SCRIPT = Path(__file__).resolve().parent / "utils" / "socks_relay.py"
RELAY_READY_TIMEOUT = 15.0


def lock_file(*, create: bool = False) -> Path:
    """The CDP lock, registry name `browser-cdp-lock`, resolved at call time.

    `create=False` is the default because most callers only ask whether the
    file exists, and a read must not create the store directory (2026-09-22).
    `_write_lock` passes `create=True` so `data_write_file` creates the store
    root at 0700, not under the process umask (MEASURED 2026-09-22).
    """
    if create:
        return data_write_file("browser-cdp-lock")
    return data_path("browser-cdp-lock")


def launch_log() -> Path:
    # Browser stdout/stderr goes here instead of the caller's streams. Truncated
    # on every launch, so it holds the current session only and cannot grow
    # unbounded. The DIRECTORY is the registry name: the engine's default set
    # may not name a `.log` file, and an edit to `browser-logs` moves this file.
    return data_write_dir("browser-logs") / "browser-launch.log"


def _active_lock_file() -> Optional[Path]:
    """Return the lock file if it exists."""
    lock = lock_file()
    return lock if lock.exists() else None


def _browser_paths(browser: str = DEFAULT_BROWSER) -> dict:
    """Resolve per-OS exe + user_data + process_name for a browser.

    Returns a dict {exe: Path, user_data: Path, process_name: str}.
    Raises ValueError if the browser is unknown or unsupported on this OS.
    Resolves at call time so importing this module on a platform without the
    browser does not crash.
    """
    cfg = _BROWSER_CONFIGS.get(browser.lower())
    if cfg is None:
        raise ValueError(
            f"Unknown browser '{browser}'. Supported: {sorted(_BROWSER_CONFIGS)}"
        )
    plat_cfg = cfg.get(sys.platform)
    if plat_cfg is None:
        # Linux has no Comet build, etc.
        raise ValueError(
            f"Browser '{browser}' is not supported on platform '{sys.platform}'. "
            f"Try one of: {sorted(_BROWSER_CONFIGS)}"
        )
    return {
        "exe": Path(plat_cfg["exe"]).expanduser(),
        "user_data": Path(plat_cfg["user_data"]).expanduser(),
        "process_name": plat_cfg["process_name"],
    }


def _log(msg: str, color: str = CYAN) -> None:
    print(f"{color}[browser]{RESET} {msg}", flush=True)


def _cdp_ready(port: int, timeout: float = 1.0) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _port_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


def is_running(browser: str = DEFAULT_BROWSER) -> bool:
    """Return True if the named browser has at least one running process.

    Cross-platform: uses `tasklist` on Windows, `pgrep` on POSIX.
    """
    try:
        process_name = _browser_paths(browser)["process_name"]
    except ValueError:
        return False

    if sys.platform == "win32":
        try:
            out = subprocess.check_output(["tasklist"], text=True, errors="ignore")
        except Exception as exc:
            _log(f"tasklist failed ({exc}); treating {browser} as not running.", YELLOW)
            return False
        target = process_name.lower()
        return any(line.lower().startswith(target) for line in out.splitlines())

    # POSIX: match against the comm field (basename, no flag), not the full
    # command-line. With `-f` even an unrelated process whose argv contains the
    # substring 'brave' (a path component, a python import line, anything) would
    # return a false positive — verified empirically 2026-05-23 on WSL2.
    try:
        result = subprocess.run(
            ["pgrep", process_name],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except FileNotFoundError:
        _log(f"pgrep not available; cannot tell whether {browser} is running.", YELLOW)
        return False


def _write_lock(port: int, pid: int, browser: str, relay: Optional[dict] = None) -> None:
    """Record the CDP session so `stop` can find it. Atomic, because a
    truncated lock leaves Windows nothing to signal on `stop` (2026-08-24).
    """
    state = {"port": port, "pid": pid, "browser": browser}
    if relay:
        state["relay"] = relay
    atomic_write_text(lock_file(create=True), json.dumps(state, indent=2) + "\n")


def _adopt_running_cdp(port: int, browser: str) -> int:
    """Reuse a CDP endpoint that is already up, or refuse it.

    `_cdp_ready` answers for ANY Chromium-family process on the port, so the
    owner is checked too, or `attach()` would drive the wrong browser (2026-08-24).
    """
    owners = _pids_for_cdp_port(port)
    mine = [pid for pid in owners if _pid_is_browser(pid, browser)]
    if owners and not mine:
        raise RuntimeError(
            f"Port {port} already serves CDP, but PID {owners[0]} is not "
            f"{browser}. Stop that browser, or launch on another --port."
        )
    if not mine:
        # No `ps` to read (Windows) or the owner's cmdline is unreadable. The
        # endpoint answers and we cannot say whose it is; say that, rather than
        # record a lock naming a browser nothing verified.
        _log(f"CDP already ready on port {port}, but this platform cannot "
             f"identify its owner; reusing it without a lock file.", YELLOW)
        return 0
    pid = mine[0]
    _write_lock(port, pid, browser)
    _log(f"CDP already ready on port {port} (PID {pid}, {browser}); reusing.", GREEN)
    return pid


def _abandon_launch(proc: subprocess.Popen, port: int, browser: str) -> None:
    """Kill a browser we launched that never opened its CDP port.

    Otherwise an untracked browser on the authenticated profile keeps an open
    loopback debug port that `stop` cannot find (2026-08-24).
    """
    for pid in _pids_for_cdp_port(port):
        if pid == proc.pid:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            _log(f"Launch timed out; sent SIGTERM to CDP owner PID {pid}", YELLOW)
        except ProcessLookupError:
            _log(f"Launch timed out; CDP owner PID {pid} already gone", GRAY)
        except OSError as e:
            _log(f"Launch timed out; SIGTERM to PID {pid} failed: {e}", RED)
    with contextlib.suppress(OSError):
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            proc.kill()
        _log(f"Launch timed out; killed {browser} launcher PID {proc.pid}", YELLOW)
    else:
        _log(f"Launch timed out; terminated {browser} launcher PID {proc.pid}", YELLOW)


def relay_log() -> Path:
    """The relay child's stderr. Truncated on every launch, like `launch_log`."""
    return data_write_dir("browser-logs") / "socks-relay.log"


def _start_relay(slot: int) -> Tuple[subprocess.Popen, int]:
    """Start the loopback relay for `slot` and return (process, port), or raise.

    Every failure raises, and the caller launches nothing: the child printing
    an ERROR line, exiting, staying silent past the timeout, or answering with
    a port nothing is listening on. The interpreter is the pinned venv one,
    never a bare `python`.
    """
    from scripts.utils.venv_guard import venv_python
    log_path = relay_log()
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(
            [str(venv_python()), str(RELAY_SCRIPT), "--slot", str(slot)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
            start_new_session=True,
        )
    # Read with a deadline on the raw fd: `readline` after `select` blocks with
    # no timeout until a newline arrives.
    buf = b""
    deadline = time.time() + RELAY_READY_TIMEOUT
    fd = proc.stdout.fileno()
    while b"\n" not in buf and time.time() < deadline:
        ready, _, _ = select.select([fd], [], [], max(0.0, deadline - time.time()))
        if not ready:
            break
        chunk = os.read(fd, 256)
        if not chunk:
            break
        buf += chunk
    line = buf.split(b"\n", 1)[0].decode(errors="replace").strip()
    words = line.split()
    port = int(words[1]) if len(words) == 2 and words[0] == "READY" and words[1].isdigit() else 0
    if not port or not _port_listening(port):
        exited = proc.poll()
        _stop_relay_proc(proc)
        detail = line or (f"relay exited with code {exited} before answering" if exited is not None
                          else f"no answer within {RELAY_READY_TIMEOUT:.0f}s")
        raise RuntimeError(f"proxy relay for DECODO_PROXY_{slot} did not come up ({detail}; "
                           f"log: {log_path}). Browser NOT launched.")
    return proc, port


def _stop_relay_proc(proc: subprocess.Popen) -> None:
    with contextlib.suppress(OSError):
        proc.stdin.close()
    with contextlib.suppress(OSError):
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)


def _pid_is_relay(pid: int) -> bool:
    """True only for a live process running THIS relay script (PID-reuse guard).

    The script's absolute path as an argument AND `--slot` beside it, so an
    editor or a grep that merely names the file does not verify as the relay.
    """
    args = _pid_cmdline(pid).split()
    return str(RELAY_SCRIPT) in args and "--slot" in args


def launch_browser(
    browser: str = DEFAULT_BROWSER,
    port: int = DEFAULT_PORT,
    initial_url: Optional[str] = None,
    profile_folder: str = PROFILE_FOLDER,
    wait_timeout: float = 30.0,
    proxy_slot: int = 0,
) -> int:
    """Launch the chosen browser externally with CDP enabled. Returns PID.

    The `browser` parameter selects between supported Chromium-family browsers.
    It defaults to `DEFAULT_BROWSER` ('brave'), which is the primary on every
    platform; 'comet' is a Windows/macOS-only opt-in with no Linux build.

    Refuses to launch if the browser is already running — CDP won't attach to
    an already-owned profile — and refuses to reuse a CDP port owned by some
    OTHER browser.

    The returned PID is 0 in exactly one case: the port already served CDP and
    this platform could not identify the owning process (see
    `_adopt_running_cdp`).

    With `proxy_slot`, the relay is started first and the browser is launched
    only once it answers; a browser already serving CDP is refused rather than
    adopted, because nothing can put a running browser behind a proxy.
    """
    if _cdp_ready(port):
        if proxy_slot:
            raise RuntimeError(f"CDP is already served on port {port}; a running browser "
                               f"cannot be put behind DECODO_PROXY_{proxy_slot}. Stop it first.")
        return _adopt_running_cdp(port, browser)

    paths = _browser_paths(browser)

    if is_running(browser):
        _log(f"{browser} is already running. Close it fully (including tray) "
             "before launching with CDP. Aborting.", RED)
        raise RuntimeError(f"{browser} already running; CDP attach requires fresh launch")

    exe = paths["exe"]
    if not exe.exists():
        raise FileNotFoundError(f"{browser} not found at {exe}")

    cmd = [
        str(exe),
        f"--remote-debugging-port={port}",
        f"--profile-directory={profile_folder}",
        f"--user-data-dir={paths['user_data']}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    relay_proc = None
    relay_port = 0
    if proxy_slot:
        relay_proc, relay_port = _start_relay(proxy_slot)
        # A relay that listens but cannot authenticate upstream would give the
        # browser no network at all; ask the exit IP through it first.
        exit_ip = _exit_ip(f"socks5h://127.0.0.1:{relay_port}")
        if exit_ip.startswith("<"):
            _stop_relay_proc(relay_proc)
            raise RuntimeError(f"DECODO_PROXY_{proxy_slot} does not carry traffic through the "
                               f"relay: {exit_ip}. Browser NOT launched.")
        _log(f"Relay for DECODO_PROXY_{proxy_slot} up on 127.0.0.1:{relay_port} "
             f"(PID {relay_proc.pid}), exit IP {exit_ip}", GREEN)
        cmd += [f"--proxy-server=socks5://127.0.0.1:{relay_port}", *PROXY_WEBRTC_FLAGS]
    if initial_url:
        cmd.append(initial_url)

    _log(f"Launching: {' '.join(cmd)}")
    # Detach the browser's standard streams from the caller's. A browser that
    # inherits them holds the caller's stdout for its ENTIRE lifetime, so a
    # pipeline never reaches EOF: on 2026-07-28 a
    # `browser.py launch ... 2>&1 | tail -20` shell stayed alive 12.5 hours
    # after `timeout 90` had already killed python, because /usr/bin/brave-browser
    # is a shell wrapper whose `cat` helpers keep that write end open long after
    # the launcher itself exits. Writing to a file gives the child its own fds.
    log_path = launch_log()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log
        )
    _log(f"{browser} PID: {proc.pid} (output -> {log_path})", GREEN)

    deadline = time.time() + wait_timeout
    while time.time() < deadline and not _cdp_ready(port):
        time.sleep(0.5)
    if not _cdp_ready(port):
        _abandon_launch(proc, port, browser)
        if relay_proc:
            _stop_relay_proc(relay_proc)
        raise TimeoutError(f"CDP did not become ready on port {port} within {wait_timeout}s")

    # Record the process that actually owns the CDP port, not Popen's PID. On
    # Debian/Ubuntu `/usr/bin/brave-browser` is a shell wrapper that forks the
    # real binary and exits, so Popen's PID is dead by now (and its number is
    # free to be recycled onto an unrelated process).
    owners = _pids_for_cdp_port(port)
    real_pid = owners[0] if owners else proc.pid
    if real_pid != proc.pid:
        _log(f"{browser} re-parented: launcher {proc.pid} -> browser {real_pid}", GRAY)

    relay = None
    if relay_proc:
        # Hand the relay the browser's PID and close its stdin: from here it
        # lives exactly as long as the browser does.
        try:
            relay_proc.stdin.write(f"{real_pid}\n".encode())
            relay_proc.stdin.close()
        except OSError as exc:
            # The relay died after the exit-IP check: the browser would run
            # with no network and, with no lock, nothing could stop it.
            _abandon_launch(proc, port, browser)
            _stop_relay_proc(relay_proc)
            raise RuntimeError(f"proxy relay exited before the browser could be tied to it "
                               f"({exc}); browser stopped.") from exc
        # Reap the relay when it exits, for a long-lived caller of this function.
        threading.Thread(target=relay_proc.wait, daemon=True).start()
        relay = {"pid": relay_proc.pid, "port": relay_port, "slot": proxy_slot}
    _write_lock(port, real_pid, browser, relay)
    _log(f"CDP ready on http://127.0.0.1:{port}", GREEN)
    return real_pid


@contextlib.contextmanager
def attach(port: int = DEFAULT_PORT) -> Iterator[Tuple[object, object]]:
    """Attach Playwright to an externally-launched browser. Yields (browser, context).

    On exit, drops the CDP connection but does NOT close the browser. Calling
    `browser.close()` on a CDP-attached session terminates the whole browser;
    avoid it.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise ImportError("playwright not installed. `pip install playwright && python -m playwright install chromium`") from e

    if not _cdp_ready(port):
        raise ConnectionError(
            f"No CDP endpoint on port {port}. Call launch_browser() first, "
            f"or ensure the browser was launched with --remote-debugging-port={port}."
        )

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        try:
            yield browser, ctx
        finally:
            pass  # context manager exit drops the connection; the browser stays alive


def pick_tab(ctx, url_substring: str, bring_to_front: bool = True):
    """Pick a tab from `ctx.pages` whose URL contains `url_substring`.

    Never trust `ctx.pages[0]` — Comet's Perplexity sidecar injects tabs at
    unpredictable positions.

    Raises LookupError if no matching tab is found.
    """
    for p in ctx.pages:
        u = p.url or ""
        if url_substring in u and "chrome-error" not in u:
            if bring_to_front:
                try:
                    p.bring_to_front()
                except Exception as exc:
                    print(f"browser: bring_to_front failed: {exc}", file=sys.stderr)
            return p
    open_urls = [p.url for p in ctx.pages]
    raise LookupError(
        f"No tab matching '{url_substring}'. Open tabs: {open_urls}"
    )


def _pid_cmdline(pid: int) -> str:
    """Full command line of a PID, or "" if it cannot be read.

    Reads /proc where available (Linux) and falls back to `ps` (macOS).
    """
    proc_file = Path(f"/proc/{pid}/cmdline")
    try:
        if proc_file.exists():
            return proc_file.read_bytes().replace(b"\0", b" ").decode(errors="ignore")
    except OSError:
        return ""
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="],
            capture_output=True, text=True, check=False,
        )
        return out.stdout.strip()
    except (FileNotFoundError, OSError):
        return ""


def _pid_is_browser(pid: int, browser_name: str = DEFAULT_BROWSER) -> bool:
    """True only if `pid` is live AND really is that browser.

    Guards against PID reuse: the launcher wrapper exits within milliseconds,
    so a stale tracked PID can be recycled onto an unrelated process. Signalling
    it blind would kill a bystander.
    """
    try:
        process_name = _browser_paths(browser_name)["process_name"]
    except ValueError:
        return False

    if sys.platform == "win32":
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}"], text=True, errors="ignore"
            )
        except Exception as exc:
            _log(f"tasklist failed for PID {pid} ({exc}); not verified as {browser_name}.", YELLOW)
            return False
        return process_name.lower() in out.lower()

    cmdline = _pid_cmdline(pid)
    if not cmdline:
        return False
    # Match the executable only (argv[0]), never the whole command line: a URL
    # or a path component containing the browser name would false-positive.
    return _exe_name_matches(Path(cmdline.split()[0]).name, process_name)


def _exe_name_matches(exe_name: str, process_name: str) -> bool:
    """True when `exe_name` names `process_name`'s binary, at a name boundary.

    A substring test would verify a recycled PID such as "unbrave-daemon" as
    the browser and SIGTERM it (2026-08-24); plain equality would miss the
    Debian/Ubuntu `brave-browser` binary. So: equal, or a prefix followed by a
    separator.
    """
    exe = exe_name.lower()
    want = process_name.lower()
    if exe == want:
        return True
    return exe.startswith(want) and not exe[len(want):len(want) + 1].isalnum()


def _parse_cdp_owner_pids(ps_output: str, port: int, self_pid: int) -> List[int]:
    """Pick the main browser PIDs out of `ps -eo pid=,args=` output.

    Three kinds of process carry `--remote-debugging-port=<port>` in their
    argv, and only the second is the one to signal (verified on WSL2,
    2026-07-27):

    * the launcher wrapper — on Debian/Ubuntu `/usr/bin/brave-browser` is a
      bash script. It is the PID `Popen` reports, and killing it orphans the
      browser instead of stopping it.
    * the browser itself — no `--type=` flag.
    * renderer / zygote / gpu / utility children — they inherit the flag in
      their command line and are identified by `--type=`. They die with the
      browser, so signalling them is noise at best.
    """
    flag = f"--remote-debugging-port={port}"
    pids = []
    for line in ps_output.splitlines():
        line = line.strip()
        if flag not in line:
            continue
        head, _, rest = line.partition(" ")
        if not head.isdigit():
            continue
        pid = int(head)
        if pid == self_pid:
            continue
        rest = rest.strip()
        if "--type=" in rest:            # renderer / zygote / gpu / utility
            continue
        argv0 = rest.split()[0] if rest else ""
        if Path(argv0).name in ("sh", "bash", "ps", "grep", "env", "timeout"):
            continue                      # launcher wrapper or an observer
        pids.append(pid)
    return pids


def _pids_for_cdp_port(port: int) -> List[int]:
    """PIDs of the main browser process(es) holding `port` for CDP.

    Authoritative because it survives the Debian/Ubuntu wrapper forking away
    from the PID that Popen reported.

    Windows returns an empty list — there the launcher is the browser exe
    itself, so the verified tracked PID is already correct.
    """
    if sys.platform == "win32":
        return []
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,args="], capture_output=True, text=True, check=False
        )
    except (FileNotFoundError, OSError) as exc:
        _log(f"ps failed ({exc}); cannot identify the CDP owner on port {port}.", YELLOW)
        return []
    return _parse_cdp_owner_pids(out.stdout, port, os.getpid())


def _wait_until_cdp_down(port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while True:
        if not _cdp_ready(port):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.25)


def _stop_relay(state: dict) -> None:
    """Stop the relay a lock records, once its PID verifies as the relay."""
    relay = state.get("relay") if isinstance(state.get("relay"), dict) else None
    pid = relay.get("pid") if relay else None
    if not isinstance(pid, int):
        return
    if not _pid_is_relay(pid):
        _log(f"Relay PID {pid} is not running the relay; nothing to stop.", GRAY)
        return
    try:
        os.kill(pid, signal.SIGTERM)
        _log(f"Sent SIGTERM to relay PID {pid}", GREEN)
    except ProcessLookupError:
        _log(f"Relay PID {pid} already gone", GRAY)
    except OSError as e:
        _log(f"SIGTERM to relay PID {pid} failed: {e}", RED)


def _clear_lock(lock) -> None:
    """Remove the lock file if there is one. No-op when stopping by port alone."""
    if lock is None:
        return
    with contextlib.suppress(OSError):
        lock.unlink()


def stop_browser(port: Optional[int] = None, timeout: float = 10.0) -> bool:
    """Stop the CDP browser session. Returns True only if it actually stopped.

    Terminates every process holding the CDP port (plus the tracked PID when it
    verifies as the browser), waits for the port to close, escalates to SIGKILL
    if SIGTERM is ignored, and clears the lock file only on confirmed shutdown.
    A surviving browser leaves the lock in place so the caller can retry.

    `port` alone is a complete instruction ON POSIX: with no lock file this
    stops whatever holds that port, because `_pids_for_cdp_port` can name the
    owner from `ps`. It is NOT complete on Windows, where that function returns
    an empty list by design and the owner can only come from the lock, so an
    unidentifiable owner there is refused in one line rather than waited out.
    """
    lock = _active_lock_file()
    # An explicit `port` is enough on its own. A missing lock used to end the
    # function here, which made a session UNSTOPPABLE from the CLI in the two
    # states this file already documents: `_adopt_running_cdp` reuses a live
    # endpoint "without a lock file", and an unreadable lock leaves no tracked
    # PID to recover on Windows. With a port named, there is something to aim
    # at whether or not a lock exists.
    if lock is None and port is None:
        _log("No lock file and no port given; nothing tracked to stop. "
             "Pass --port to stop an untracked session.", YELLOW)
        return False

    state = {}
    if lock is None:
        _log(f"No lock file; stopping whatever holds CDP port {port}.", YELLOW)
    else:
        try:
            state = json.loads(lock.read_text())
        except (OSError, ValueError) as e:
            # Never silent: an unreadable lock means the tracked PID is gone, and
            # on Windows `_pids_for_cdp_port` cannot recover it, so `stop` is
            # about to signal nothing at all. Say which file and why.
            _log(f"Lock {lock.name} is unreadable ({e}); falling back to whatever "
                 f"holds the CDP port.", YELLOW)
        if not isinstance(state, dict):
            _log(f"Lock {lock.name} does not hold an object; ignoring its contents.", YELLOW)
            state = {}
    port = port or state.get("port") or DEFAULT_PORT
    browser_name = state.get("browser") or DEFAULT_BROWSER
    tracked = state.get("pid")

    targets = list(_pids_for_cdp_port(port))
    if tracked and tracked not in targets and _pid_is_browser(tracked, browser_name):
        targets.append(tracked)
    elif tracked and tracked not in targets:
        _log(f"Tracked PID {tracked} is not {browser_name}; ignoring it.", GRAY)

    if not targets and not _cdp_ready(port):
        _log("Browser already stopped; clearing lock." if lock
             else f"Nothing is holding CDP port {port}.", GREEN)
        _stop_relay(state)
        _clear_lock(lock)
        return True

    # Serving, and nothing to aim at. On POSIX that is a transient race worth
    # signalling through; on Windows it is a permanent state, because
    # `_pids_for_cdp_port` returns [] there by design and the tracked PID is the
    # only owner this file can ever recover. Signalling an empty list twice and
    # waiting out both timeouts told the operator nothing after fifteen seconds.
    # Refuse in one line instead, and name the two ways out.
    if not targets and sys.platform == "win32":
        _log(f"CDP port {port} is serving but this platform cannot identify "
             f"the process holding it, and no usable lock file names one. "
             f"Close the browser window, or stop the process by hand "
             f"(`netstat -ano | findstr :{port}` gives its PID).", RED)
        return False

    sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
    for sig, label, wait in ((signal.SIGTERM, "SIGTERM", timeout),
                             (sigkill, "SIGKILL", min(timeout, 5.0))):
        for pid in targets:
            try:
                os.kill(pid, sig)
                _log(f"Sent {label} to PID {pid}", GREEN)
            except ProcessLookupError:
                _log(f"PID {pid} already gone", GRAY)
            except OSError as e:
                _log(f"{label} to PID {pid} failed: {e}", RED)
        if _wait_until_cdp_down(port, wait):
            _log(f"CDP port {port} closed; browser stopped.", GREEN)
            _stop_relay(state)
            _clear_lock(lock)
            return True
        if sig is not sigkill:
            _log(f"Still up after {label}; escalating.", YELLOW)

    _log(f"Browser still answering on port {port}; lock kept for retry.", RED)
    return False


def cmd_launch(args: argparse.Namespace) -> int:
    # The route is settled first: no relay, no browser and no lock before it.
    slot = resolve_route(args.proxy_slot, tool="browser.py launch")
    if slot is None:
        return EXIT_ROUTE_NOT_CHOSEN
    try:
        launch_browser(
            browser=args.browser,
            port=args.port,
            initial_url=args.url,
            profile_folder=args.profile,
            proxy_slot=slot,
        )
    except Exception as e:
        _log(str(e), RED)
        return 1
    return 0


def _lock_state() -> dict:
    """The recorded session, or an empty dict. Never raises."""
    lock = _active_lock_file()
    if lock is None:
        return {}
    try:
        state = json.loads(lock.read_text())
    except (OSError, ValueError) as exc:
        _log(f"Lock {lock.name} is unreadable ({exc}); treating it as empty.", YELLOW)
        return {}
    return state if isinstance(state, dict) else {}


def _exit_ip(proxy: Optional[str]) -> str:
    """The address `IP_CHECK_URL` sees, through `proxy` or direct; an error as text."""
    try:
        import requests
        proxies = {"http": proxy, "https": proxy} if proxy else None
        return requests.get(IP_CHECK_URL, proxies=proxies, timeout=15,
                            headers={"User-Agent": user_agent()}).json()["ip"]
    except Exception as exc:
        return f"<failed: {type(exc).__name__}: {exc}>"


def proxy_status(state: dict) -> bool:
    """Report the recorded relay; True only when every check holds.

    Three checks, each of which can be false on its own: the browser's command
    line names this relay's port (a browser launched without the flag is direct
    whatever the relay does), the relay's port answers, and the exit IP through
    the relay differs from the direct one.
    """
    relay = state.get("relay") if isinstance(state.get("relay"), dict) else None
    if not relay:
        _log("Proxy: none recorded; this session goes out direct.", YELLOW)
        return True
    port, slot, pid = relay.get("port"), relay.get("slot"), state.get("pid")
    flag = f"--proxy-server=socks5://127.0.0.1:{port}"
    flagged = isinstance(pid, int) and flag in _pid_cmdline(pid)
    listening = isinstance(port, int) and _port_listening(port)
    _log(f"Proxy slot {slot}: browser PID {pid} launched with {flag}: {flagged}")
    _log(f"Proxy relay 127.0.0.1:{port} (PID {relay.get('pid')}) listening: {listening}")
    via = _exit_ip(f"socks5h://127.0.0.1:{port}") if listening else "<relay down>"
    direct = _exit_ip(None)
    proxied = listening and not via.startswith("<") and via != direct
    _log(f"Exit IP through the relay: {via}; direct: {direct}; proxied: {proxied}",
         GREEN if proxied else RED)
    return flagged and listening and proxied


def cmd_routes(args: argparse.Namespace) -> int:
    """Print the four routes with their measured exit; start nothing."""
    print("\n".join(format_menu(probe_all())))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    # Probe the port the session is actually on, not DEFAULT_PORT (2026-08-24).
    port = args.port or _lock_state().get("port") or DEFAULT_PORT
    running = is_running(args.browser)
    port_open = _port_listening(port)
    cdp = _cdp_ready(port)
    _log(f"{args.browser} running: {running}")
    _log(f"CDP port {port} listening: {port_open}")
    _log(f"CDP endpoint reachable: {cdp}")
    lock = _active_lock_file()
    if lock is not None:
        # ValueError too: a non-UTF-8 lock raises UnicodeDecodeError (MEASURED 2026-09-01).
        try:
            body = lock.read_text().strip()
        except (OSError, ValueError) as exc:
            body = f"<unreadable: {exc}>"
        _log(f"Lock file ({lock.name}): {body}")
    proxy_ok = proxy_status(_lock_state())
    return 0 if cdp and proxy_ok else 2


def cmd_stop(args: argparse.Namespace) -> int:
    # `--port` stops a session with no lock file on POSIX; Windows refuses (2026-08-24).
    return 0 if stop_browser(port=getattr(args, "port", None)) else 1


def main() -> int:
    p = argparse.ArgumentParser(description="Chromium-family browser CDP launcher/attach helper.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("launch", help="Launch a Chromium-family browser with CDP enabled")
    sp.add_argument("--port", type=int, default=DEFAULT_PORT)
    sp.add_argument("--url", default=None, help="Initial URL to load")
    sp.add_argument("--profile", default=PROFILE_FOLDER,
                    help="Browser profile folder name (default 'Default' — display 'ClaudeCode')")
    sp.add_argument("--browser", default=DEFAULT_BROWSER,
                    choices=sorted(_BROWSER_CONFIGS),
                    help=f"Which browser to launch (default '{DEFAULT_BROWSER}'). "
                         "Brave is the cross-platform primary; 'comet' is a "
                         "Windows/macOS-only opt-in fallback.")
    add_route_argument(sp, "A slot sends the browser through its SOCKS5 proxy via a "
                           "loopback relay tied to the browser's lifetime; the launch "
                           "refuses rather than going direct if the relay does not come up.")
    sp.set_defaults(func=cmd_launch)

    sp = sub.add_parser("routes", help="Show direct and proxy slots 1-3 with their measured "
                                       "exit IP and country; starts nothing")
    sp.set_defaults(func=cmd_routes)

    sp = sub.add_parser("status", help="Report CDP endpoint state")
    sp.add_argument("--browser", default=DEFAULT_BROWSER,
                    choices=sorted(_BROWSER_CONFIGS),
                    help="Which browser to check (default '%(default)s')")
    sp.add_argument("--port", type=int, default=None,
                    help=f"CDP port to probe (default: the lock file's port, "
                         f"else {DEFAULT_PORT})")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("stop", help="Terminate the tracked browser CDP session")
    sp.add_argument("--port", type=int, default=None,
                    help=f"CDP port to stop (default: the lock file's port, "
                         f"else {DEFAULT_PORT})")
    sp.set_defaults(func=cmd_stop)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
