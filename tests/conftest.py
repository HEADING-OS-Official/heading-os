"""Root test configuration.

Pin the per-instance timezone for the whole test session so tests that assert
local-time behaviour (calendar, scheduling, daemon heartbeats) validate the
real Etc/GMT-4 logic rather than the engine's UTC default. The production
value lives in the gitignored .env; here we set it deterministically.
See scripts.utils.workspace.get_default_tz().

This file also holds the re-exec guard for the whole suite; see below.
"""
import os
import tempfile
from pathlib import Path

import pytest

from scripts.utils import runtime_dir as _runtime_dir, venv_guard as _venv

# `pytester` drives a CHILD pytest over a scratch tree, which is the only way to
# assert what this file does to a test that leaks: the leaking test has to exist
# and must not be collected by the suite proper. Registered here because a
# root conftest is the one place `pytest_plugins` is allowed.
# The logging tree is process-wide state pytest does not restore between tests,
# and a test that leaves it configured fails a LATER test under the later
# test's name. The guard that snapshots it before each test, diffs after, and
# errors the culprit at teardown lives in `scripts/utils/logging_tree_guard.py`
# (MEASURED 2026-09-13, recorded in its docstring) and is armed here as a plugin
# rather than copied in, so its bidirectional test can arm the SAME module in a
# child pytest. `pytester` is pytest's own bundled plugin for that child run; it
# is not autoloaded, and this is the one place a `pytest_plugins` line may sit.
# `scripts/utils/sys_path_guard.py` is the same shape for `sys.path`; it was a
# pair of hook functions in this file until 2026-09-13, when a merge gave this
# module two `pytest_runtest_setup` definitions and Python kept the other one.
# A guard that adds a `pytest_*` hook goes in its own module on this list.
pytest_plugins = [
    "scripts.utils.logging_tree_guard",
    "scripts.utils.sys_path_guard",
    "scripts.utils.runtime_write_guard",
    "scripts.utils.skip_ceiling_guard",
    "pytester",
]

# Assignment, not setdefault, as with HEADING_OS_RUNTIME_REDIRECT further
# down: a pin a stray shell variable can switch off is not a pin.
# It was a `setdefault` until 2026-08-30, so `HEADING_OS_TZ=America/New_York
# pytest tests/` quietly re-pointed every calendar, scheduling and heartbeat
# assertion at New York offsets while this docstring still promised Etc/GMT-4,
# and nothing in the run said the substitution had happened. A test that needs
# a different zone sets it per case with monkeypatch, which applies inside the
# test body and still wins.
os.environ["HEADING_OS_TZ"] = "Etc/GMT-4"

# The suite's re-exec guard, set ONCE, here, because this file is collected
# before any test module.
#
# About twenty scripts call `ensure_venv()` at module scope and about twenty test
# modules load a script by path. When the running interpreter is not
# .venv/bin/python, that call `os.execv`s the WHOLE pytest process, which inherits
# pytest's capture as file descriptor 1: the relaunched run writes every byte
# into a temp file nobody reads, so `pytest tests/` prints ZERO bytes while
# exiting 0 on a passing set and 1 on a failing one. Measured on this repository,
# 2026-07-26. A run that prints nothing is indistinguishable from one that never
# happened.
#
# It lived as three per-module copies until wire 2.2, and that shape could not
# hold. The variable is process-global, so each copy satisfied the other two
# modules' guard tests: deleting the line from one module left its own test
# passing. Worse, the defect was self-erasing, because a NEW unguarded module
# re-execs at collection, `ensure_venv` sets this same sentinel before
# `os.execv`, and in the relaunched silent run every guard test passes.
#
# The constant is referenced rather than spelled out: a duplicated literal drifts
# silently the day venv.py renames it.
os.environ.setdefault(_venv._SENTINEL, "1")

_TESTS_ROOT = Path(__file__).resolve().parent
_ENGINE_ROOT = _TESTS_ROOT.parent

# Runtime logs written during a test run go to their own directory, never to the
# operator's. Measured the day the denial counter landed (2026-08-01): a single
# suite run appended 13 refusals to the production `.logs/denials/denials.jsonl`
# from tests that legitimately exercise leak-guard and the push walls with
# fixtures. Left alone, the instrument built to decide which guards earn their
# cost would have counted its own test suite as the workspace's main offender,
# which is the same defect class as an instrument with a silently wrong
# denominator — the thing this counter exists to end, reproduced inside it.
#
# Assignment, not setdefault: isolation that a stray shell variable can switch
# off is not isolation. Individual tests still redirect per-case with monkeypatch
# or an explicit subprocess env, and both continue to win over this.
#
# WHERE, since 2026-09-25. This isolation and the two below lived under the
# engine checkout's `.logs/_pytest/` until HOS-5 section 7 took every artifact out
# of the checkout (operator decision that day: "old way should go out and do not
# stay in the workspace"). The logs a test writes now go to the data repository's
# runtime store like everyone else's, so the redirect is the store's own:
# `HEADING_OS_RUNTIME_REDIRECT` sends the operator's data root's store to a
# directory under THIS checkout's scratch, and a test over its own scratch data
# root is untouched by it (`scripts/utils/runtime_dir.py::ENV_REDIRECT`). Where
# that directory is, and why a child pytest lands in the same one, is
# `runtime_dir.test_session_store`, beside the variable's own format.
_TEST_RUNTIME, _TEST_RUNTIME_REDIRECT = _runtime_dir.test_session_store(_ENGINE_ROOT, Path(tempfile.gettempdir()))
os.environ["HEADING_OS_RUNTIME_REDIRECT"] = _TEST_RUNTIME_REDIRECT

# The same isolation, one guard along. `check_rate_limit` in the PreToolUse
# dispatcher counts Write and Edit calls per day and BLOCKS past 1000, and six
# test modules drive that hook in a subprocess exactly as production does.
# Measured 2026-08-07, before this line: the operator's counter stood at 1033
# and was blocking three tests, and the writes it had stored were fixtures —
# `threads/personal/foo.md`, a Windows path that cannot exist on this machine,
# a scratch probe file. One run of three of those modules added 12 more.
#
# UNLINKED at import, not merely redirected. The counter is keyed by date and
# resets itself tomorrow, but a run this hour would otherwise inherit every
# earlier run's fixtures today, and enough runs in one day would reproduce the
# same block in the new location. Starting from zero is what makes the
# redirection a fix rather than a move.
#
# Only the session that OWNS the variable resets it, and that distinction was
# measured rather than reasoned. This suite spawns pytest CHILDREN — the Canopus
# probe alone runs several per contract — and each child imports this file. With
# an unconditional unlink they wiped their parent's counter mid-run, so after a
# full run the file was simply gone and the reset the comment promised was not
# the reset the code performed. An inherited variable means a parent already did
# this; the child leaves it alone.
_TEST_RATE_STATE = _TEST_RUNTIME / "dispatch-rate.json"
_OWNS_RATE_STATE = "WS_RATE_LIMIT_STATE" not in os.environ
os.environ["WS_RATE_LIMIT_STATE"] = str(_TEST_RATE_STATE)
if _OWNS_RATE_STATE:
    try:
        _TEST_RATE_STATE.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:  # pragma: no cover - reported, never fatal to the run
        print(f"[conftest] could not reset the test rate-limit state: {exc}")

# The same isolation, one directory wider, and the one that was missing.
#
# `.claude/state/` sits under the ENGINE root, not the data overlay, so the
# `HEADING_OS_DATA` pin every isolated test in this suite relies on does not
# redirect it and never did. The evidence was on disk on 2026-09-01, written by
# this suite into the operator's live tree: `checkpoint-True.json` and
# `checkpoint-3.json` from the malformed-payload sweep's BAD_FIELD_VALUES,
# `checkpoint-sweep-session.json` from that file's own base payload, and
# `checkpoint-session.json` from its `[]` and `{}` cases collapsing onto a
# shared bucket.
#
# `CLAUDE_PROJECT_DIR` looked like the seam and is not. `project_root()` reads
# `payload["cwd"]` BEFORE it reads any environment variable, and that sweep
# sends `"cwd": <the engine root>` in every payload, so the pin those tests
# thought they held was beaten before it was read. `HEADING_OS_STATE_DIR` is a
# separate question asked ahead of the payload; `checkpoint_paths.state_root()`
# carries the full reasoning.
#
# Assignment, not setdefault, for the reason given two guards up. The owner
# check is the same one `WS_RATE_LIMIT_STATE` needs and for the same measured
# reason: this suite spawns pytest CHILDREN, each imports this file, and an
# unconditional reset would wipe a parent's state mid-run.
#
# A session directory rather than a per-test `tmp_path`, matching
# `WS_RATE_LIMIT_STATE`: checkpoint state is session-scoped and several tests
# drive a hook in one subprocess and read the result in another, so a store that
# emptied between tests would break them. It sat inside the clone under the
# gitignored `.logs/_pytest/` until 2026-09-25; it is under `_TEST_RUNTIME` now,
# outside the engine checkout.
_TEST_STATE_DIR = _TEST_RUNTIME / "claude-state"
os.environ["HEADING_OS_STATE_DIR"] = str(_TEST_STATE_DIR)


# The same isolation, applied to the one instrument the suite could falsify from
# the outside. A Healthchecks deadman answers "is that daemon alive?", and this
# repository was answering for it. `_main_loop` in scripts/inbox_pulse/daemon.py
# pinged its deadman URL (an `hc_ping` env var) at the end of a clean cycle, eleven tests then
# in the suite drove that loop to completion, and any earlier
# test that calls the real load_env() puts the production ping URL in os.environ
# for the rest of the session. Measured 2026-08-18: one run of that one file sent
# 14 real success pings to the live check. The daemon behind it had been wedged
# since 2026-08-17 06:53 UTC and had not completed a single cycle in 33 hours,
# yet the operator saw the alert clear and re-fire nine times, because every push
# ran the suite and the suite pinged. The outage was found by reading a traceback,
# not by the monitor built to find it.
#
# Blanked, not deleted, and that distinction is the whole guard: load_env() uses
# os.environ.setdefault, so a name it finds already present is left alone, while a
# deleted one is restored the moment any test loads the environment. An empty
# value is also the documented no-op for ping(), which returns False on a falsy
# URL without opening a socket.
#
# Matched on the VALUE rather than the name, because the invariant is "this
# string is a live deadman", not "this key happens to be spelled _HC_". A ping URL
# added tomorrow under any name is contained on the day it is added.
_ENV_FILE = _ENGINE_ROOT / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        _key, _, _value = _line.partition("=")
        if _ and "hc-ping.com" in _value:
            os.environ[_key.strip()] = ""

# The same containment, applied to the second instrument the suite could drive
# from the outside: the notifications bot. A deadman ping is believed; a Telegram
# message is READ, and the operator cannot tell one sent by a daemon from one
# sent by a test run.
#
# Measured 2026-08-19. `_notify_stall` in .claude/hooks/checkpoint-offer.py sends
# "HEADING OS: unattended run stopped. <reason>" when the no-progress fuse trips,
# and a test then in the suite called `_pause_unattended` directly. It deleted HEADING_OS_TELEGRAM_CHAT_ID, a name `_notify_stall` never
# reads: the function walks CHECKPOINT_ / OPS_RADAR_ / ODIN_CADENCE_
# _TELEGRAM_TARGET, and any earlier test that called the real `load_env()` had
# already put ODIN_CADENCE_TELEGRAM_TARGET in os.environ for the rest of the
# session. Every run of the suite therefore sent the operator one real alert
# about an unattended run that had never started. He received nine of them in one
# afternoon and read them as a failing feature.
#
# Matched on the NAME here, not the value, because a chat id is a bare number
# with nothing distinctive to match on. Two rings: the targets, which the callers
# check first, and the bot token, without which `telegram_notify.notify()`
# returns False before it opens a socket whatever target survives.
#
# NOT contained here: TELEGRAM_API_ID / _API_HASH / _PHONE, the credentials of
# the operator's own Telegram ACCOUNT rather than the bot. No test in this suite
# drives that client, so blanking them would be a guard against nothing; a test
# that starts driving it needs its own containment, and it is named here so that
# day is not a discovery.
_MUTED_TELEGRAM = {"TELEGRAM_NOTIFY_BOT_TOKEN"}
_MUTED_TELEGRAM.update(k for k in os.environ if k.endswith("_TELEGRAM_TARGET"))
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        _key, _, _value = _line.partition("=")
        _key = _key.strip()
        if _ and _key.endswith("_TELEGRAM_TARGET"):
            _MUTED_TELEGRAM.add(_key)
_MUTED_TELEGRAM = tuple(sorted(_MUTED_TELEGRAM))


def _mute_telegram_targets() -> None:
    """Blank every notification target visible right now, plus the bot token.

    `_MUTED_TELEGRAM` is frozen at import from this machine's `.env` and its
    inherited environment, so it names only the targets this machine actually
    configures. On the operator's laptop that is ODIN_CADENCE_ and SENTINEL_ and
    nothing else - CHECKPOINT_TELEGRAM_TARGET and OPS_RADAR_TELEGRAM_TARGET, the
    first two names the stall fuse reads, are absent from it.

    A name absent from the frozen list is a name nothing re-blanks, and that is
    a leak the fixture below cannot see. A test that removes such a name with
    `monkeypatch.delenv(name, raising=False)` gets NO undo entry: pytest's
    `MonkeyPatch.delitem` returns early when the name is already absent, before
    it appends to `_setitem`. A `load_env()` inside that test then CREATES the
    name, and nothing ever takes it back. MEASURED 2026-08-31: one test (since
    parked) left CHECKPOINT_TELEGRAM_TARGET and OPS_RADAR_TELEGRAM_TARGET at a
    fixture value for the rest of the session, and a later containment test
    failed on both in a full serial run while passing when collected alone.

    So the second loop sweeps by SUFFIX over the live environment rather than
    over the frozen list, which is what makes the containment independent of
    which names this particular machine happens to have configured. The frozen
    list still runs first: it carries the bot token, whose name does not end in
    _TELEGRAM_TARGET.
    """
    for _name in _MUTED_TELEGRAM:
        os.environ[_name] = ""
    # Materialised before the writes: os.environ is mutated in the loop body.
    for _name in [k for k in os.environ if k.endswith("_TELEGRAM_TARGET")]:
        os.environ[_name] = ""


# The migration driver's own environment is not this suite's environment.
_MIGRATION_ENV = ("HEADING_OS_2_MANIFEST", "HEADING_OS_ATTEMPT",
                  "HEADING_OS_MIGRATION_ACTOR", "HEADING_OS_SKIP_INDEX_BUILD")


def _unbind_migration_env() -> None:
    """Remove the move-day driver's variables, so a run means the same thing twice.

    MEASURED 2026-09-17: `move-day.sh` exported these four and its pre-push run
    of this suite inherited them three processes down. `HEADING_OS_2_MANIFEST`
    is the move seam's manifest override, so four tests asserting a legacy path
    went red under the driver and green by hand (1 failed with it set, 22 passed
    without, same commit).

    POPPED, not blanked: `resolve.py` treats a value naming no manifest as an
    error rather than as absence. A test that wants a manifest sets it with
    `monkeypatch.setenv`, which applies inside the test body, after this.
    """
    for _name in _MIGRATION_ENV:
        os.environ.pop(_name, None)


_mute_telegram_targets()
_unbind_migration_env()


@pytest.fixture(autouse=True)
def _isolate_runtime_logs():
    """Re-arm the redirection before every test.

    The module-scope assignment above covers import time and nothing else. A
    test that sets the variable by hand and pops it in a `finally` leaves it
    UNSET for every test that follows, and the rest of the session then writes
    to the operator's real log. That is not hypothetical: the denial counter's
    own contract does exactly that, and it is how this fixture was found.
    Restoring per test costs nothing and does not fight monkeypatch, which
    applies inside the test body, after this.

    The Telegram names are re-armed here for a sharper version of the same
    reason. Several notifier tests `monkeypatch.delenv` a target to exercise the
    unconfigured path, and a DELETED name is not a blanked one: the next
    `load_env()` anywhere in the session restores the operator's real chat id,
    because load_env uses setdefault. Blanking again before each test is what
    keeps the containment from lasting only until the first test that clears it.
    `_mute_telegram_targets` sweeps by suffix, so a target this machine does not
    configure is re-armed here too; its docstring records why that matters.
    """
    os.environ["HEADING_OS_RUNTIME_REDIRECT"] = _TEST_RUNTIME_REDIRECT
    os.environ["WS_RATE_LIMIT_STATE"] = str(_TEST_RATE_STATE)
    os.environ["HEADING_OS_STATE_DIR"] = str(_TEST_STATE_DIR)
    _mute_telegram_targets()
    # Same reason, one variable further: a test that sets HEADING_OS_2_MANIFEST
    # by hand and pops it in a `finally` leaves the NEXT test with whatever the
    # session inherited, which under the move-day driver is a real manifest.
    # Re-arming per test is what keeps the containment from lasting only until
    # the first test that touches it.
    _unbind_migration_env()
    yield


# ============================================================
# The operator's overlay is not scratch space
# ============================================================
#
# The guard itself now lives in `scripts/utils/overlay_write_guard.py`. It was
# 409 lines of this file until 2026-08-31, and a conftest arms under pytest and
# nowhere else: a scratch probe run as a plain `.venv/bin/python` overwrote a
# real operator workbook while every one of its tests was green. Moving it out
# is what lets a later change arm it in any process.
#
# NOTHING is re-exported here on purpose. Two test files reach in and replace
# `_watched_roots` and `_OVERLAY_PREFIXES` to drive the guard over a pretend
# overlay; a copy of those names sitting in this module would let such a
# replacement bind something nobody reads, and the test would pass over
# nothing. With the names absent, `monkeypatch.setattr` raises instead.
from scripts.utils import overlay_write_guard as _guard  # noqa: E402


# ============================================================
# sys.path: a test hands the interpreter back as it found it
# ============================================================
#
# The guard that snapshots `sys.path` before each item and errors the item that
# changed it lives in `scripts/utils/sys_path_guard.py`, armed through
# `pytest_plugins` at the top of this file. It sat HERE as a second
# `pytest_runtest_setup` / `pytest_runtest_teardown` pair until 2026-09-13,
# when a merge added a same-named pair elsewhere in this module and Python kept
# the last definition, so the sys.path pair was dead (MEASURED 2026-09-13).
# One implementation per hook name per module; nothing refuses a second one
# mechanically, so check this file before adding a `pytest_*` hook to it.


# ============================================================
# Egress: a unit test does not reach the internet
# ============================================================
#
# The lesson was written and the guard was never installed. The workspace's own
# record says "block `socket.connect`; one POSTed a token to Google", and on
# 2026-08-31 a sweep found no `socket` handling in any conftest at any level and
# no `pytest-socket` in the dependency set. So the rule existed in prose and the
# place it had to be applied did not have it — the same shape as the overlay
# guard above.
#
# Stdlib only, and deliberately narrow. What is refused is EGRESS: a connect to
# an address that is not loopback. What is not refused:
#
#   * loopback and AF_UNIX, always. That is local IPC (a scratch HTTP server,
#     a socketpair) and it carries nothing off the machine,
#     which is the thing this guard is about. Refusing it would break real tests
#     to buy nothing.
#   * anything under the `integration`, `acceptance` or `network` markers. The
#     first two are excluded from the per-push unit job by marker; the third is
#     the explicit opt-in for a unit test that genuinely has to leave the machine.
#
# Three primitives are guarded: `connect`, `connect_ex`, and `getaddrinfo`.
#
# This paragraph used to say the opposite. It read "`connect` is the choke point
# rather than `getaddrinfo`: a name lookup carries no payload", and on 2026-09-01
# a probe test in this suite measured that claim false: with the guard armed,
# `socket.getaddrinfo("example.com", 443)` reached a real resolver and came back
# with a real address. A hostname IS a payload, because
# `<secret>.attacker.example` leaves this machine through a resolver without
# ever touching `connect`. The wrapper landed the same day; see the measurement
# recorded at `_install_socket_guard`.
#
# The old paragraph's second half was a real cost, and it was accepted rather
# than refuted: refusing a lookup does turn "no network" into a refusal raised
# one call earlier than the connect that wanted it. `NetworkAccessRefused` names
# the host and the verb ("resolve" or "connect to"), so the message says which.

_NETWORK_MARKERS = ("network", "integration", "acceptance")
_NETWORK_ALLOWED = False        # flipped per test by _no_egress below
# `requires_ollama` is declared in `pyproject.toml` and, until 2026-09-03,
# deselected nothing and gated nothing. It is now what opens the gateway leg of
# the guard; see `_refuse_egress` for the four tests that measured a daemon.
_OLLAMA_MARKERS = _NETWORK_MARKERS + ("requires_ollama",)
_OLLAMA_ALLOWED = False         # flipped per test by _no_egress below
_RESTORE_SOCKET_GUARD = None


class NetworkAccessRefused(RuntimeError):
    """A test tried to reach a non-loopback address."""


_WSL_GATEWAY = ...          # computed once, lazily; `...` means "not yet asked"


def wsl_host_gateway():
    """The Windows side of THIS machine on WSL2, or None anywhere else.

    Under WSL2 the host is reachable only through the default-route gateway, and
    this workspace's local models live there: no ollama runs inside WSL, every
    model is pinned to the Windows host, so the embedder is a connect to
    `<gateway>:11434`. That address is not loopback, but it is not egress
    either — it is the other half of the same physical machine, and nothing
    sent to it leaves the box.

    Found by the guard rather than reasoned in advance: installing the egress
    block turned an embedder test red with a connect to the gateway on 11434.

    Derived, never hardcoded. The gateway changes on every WSL boot, so a
    literal would rot within a day. Narrowed to the single gateway ADDRESS and
    not to its subnet, and not to RFC1918 in general: the machine sits on a real
    LAN, other hosts on it are third parties, and "private address" is not a
    synonym for "this computer".
    """
    global _WSL_GATEWAY
    if _WSL_GATEWAY is not ...:
        return _WSL_GATEWAY
    _WSL_GATEWAY = None
    try:
        release = Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8")
    except OSError:
        return _WSL_GATEWAY
    if "microsoft" not in release.lower():
        return _WSL_GATEWAY
    import socket
    import struct
    try:
        rows = Path("/proc/net/route").read_text(encoding="utf-8").splitlines()[1:]
    except OSError:
        return _WSL_GATEWAY
    for row in rows:
        fields = row.split()
        if len(fields) > 2 and fields[1] == "00000000":
            try:
                _WSL_GATEWAY = socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
            except (ValueError, struct.error, OSError):
                _WSL_GATEWAY = None
            break
    return _WSL_GATEWAY


def address_is_local(address) -> bool:
    """True for loopback, AF_UNIX, the unspecified address, and the WSL2 host.

    Public so a test can drive it without opening a socket. Unparseable is
    treated as NOT local: a guard that fails open on a shape it does not
    recognise is the kind that reports clean while a token leaves the machine.
    """
    import ipaddress

    if isinstance(address, (str, bytes, os.PathLike)):
        return True                              # AF_UNIX path
    if not isinstance(address, tuple) or not address:
        return False
    host = address[0]
    if host in (None, "", "localhost"):
        return True
    try:
        text = os.fsdecode(host) if isinstance(host, bytes) else str(host)
    except (TypeError, ValueError):
        return False
    bare = text.split("%", 1)[0]
    if bare == wsl_host_gateway():
        return True
    try:
        ip = ipaddress.ip_address(bare)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_unspecified


def _is_wsl_host_gateway(address) -> bool:
    """The one address `address_is_local` calls local that is another machine."""
    if not isinstance(address, tuple) or not address:
        return False
    host = address[0]
    if isinstance(host, (str, bytes)):
        try:
            text = os.fsdecode(host) if isinstance(host, bytes) else host
        except (TypeError, ValueError):
            return False
        return text.split("%", 1)[0] == wsl_host_gateway()
    return False


def _refuse_egress(address, verb):
    if _NETWORK_ALLOWED:
        return
    # THE HOLE THIS CLOSES. `address_is_local` calls the WSL host gateway local,
    # deliberately and with a comment saying it is where the embedder lives. It
    # is the one address on that list that is a DIFFERENT MACHINE running a
    # DIFFERENT daemon, so the guard stood aside for exactly the traffic whose
    # presence or absence decides a test's verdict.
    #
    # MEASURED 2026-09-03: four unit tests reached ollama on the gateway with no
    # marker asking for it, and two of them CHANGED VERDICT when the daemon's
    # state changed and nothing else did. The same day, `scan_redundancy`'s corruption warning was
    # found to be suppressed by the same outage, and the test that should have
    # caught it had been green by luck since it was written.
    #
    # `requires_ollama` already existed in `pyproject.toml` and deselected
    # nothing. It is now the opt-in: a test that genuinely needs the daemon says
    # so and gets through here; every other test is told, loudly, at the socket.
    if _is_wsl_host_gateway(address) and not _OLLAMA_ALLOWED:
        raise NetworkAccessRefused(
            f"a test tried to {verb} {address!r}, the ollama host on the other "
            f"side of the WSL boundary. Its verdict would then be decided by "
            f"whether a foreign daemon happens to be running, not by this "
            f"repository's code. Inject the embedder (`scan_redundancy` and "
            f"`memory-index` both take one), patch "
            f"`scripts.utils.inference_host.probe`, or mark the test "
            f"`@pytest.mark.requires_ollama` if it genuinely needs the daemon."
        )
    if address_is_local(address):
        return
    raise NetworkAccessRefused(
        f"a test tried to {verb} {address!r}, which is not loopback. A unit test "
        f"must not reach the network: mock the client, or mark the test "
        f"`@pytest.mark.network` if it genuinely has to leave this machine."
    )


def _install_socket_guard():
    """Wrap the two connect primitives. Returns a callable that puts them back."""
    import socket

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guarded_connect(self, address, *args, **kwargs):
        _refuse_egress(address, "connect to")
        return real_connect(self, address, *args, **kwargs)

    def guarded_connect_ex(self, address, *args, **kwargs):
        _refuse_egress(address, "connect_ex to")
        return real_connect_ex(self, address, *args, **kwargs)

    # Set on `socket.socket`, the Python subclass, which shadows the inherited
    # `_socket.socket` methods for it and for every subclass — including
    # `ssl.SSLSocket`. `socket.create_connection`, and therefore http.client,
    # urllib, requests and urllib3, all call `sock.connect(sa)` and land here.
    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex

    # Name RESOLUTION is egress too, and wrapping only the two connect
    # primitives left it open. MEASURED 2026-09-01 with a probe test in this
    # suite: `socket.getaddrinfo("example.com", 443)` returned
    # `('104.20.23.154', 443)` with the guard armed, so a unit test asking for a
    # third-party host sent a real DNS query to a real resolver before anything
    # refused it. Two costs, and the second is the one that matters. The query
    # itself carries the hostname off this machine, and a hostname is a channel:
    # `<payload>.attacker.example` is exfiltration through a resolver that never
    # touches `connect`. The refusal now happens one call earlier, before the
    # packet, which is the same rule the rest of this guard already keeps.
    #
    # `address_is_local` answers this shape unchanged: `("localhost", 80)`,
    # `(None, port)` and every loopback literal are local, so binding helpers
    # and the WSL host gateway are untouched. MEASURED over the whole suite the
    # same day, one scratch tree, one run each: 157 failed / 20112 passed
    # without this wrapper and 157 failed / 20113 passed with it. No test in
    # this repository resolves a third-party name for a legitimate reason.
    real_getaddrinfo = socket.getaddrinfo

    def guarded_getaddrinfo(host, port, *args, **kwargs):
        _refuse_egress((host, port), "resolve")
        return real_getaddrinfo(host, port, *args, **kwargs)

    socket.getaddrinfo = guarded_getaddrinfo

    def restore():
        socket.socket.connect = real_connect
        socket.socket.connect_ex = real_connect_ex
        socket.getaddrinfo = real_getaddrinfo

    return restore


def pytest_configure(config):
    """Register the opt-in marker here, not in pyproject.

    `--strict-markers` is on, so `network` has to be declared somewhere. It is
    declared beside the guard that honours it, so the two cannot drift apart.
    """
    config.addinivalue_line(
        "markers",
        "network: this test genuinely reaches a non-loopback address "
        "(the egress guard in tests/conftest.py stands aside for it)",
    )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    """Open the gate only for a test that has asked for it, then close it again.

    A HOOK, not an autouse fixture, and the difference is the whole repair.

    MEASURED 2026-09-04 in the operator's main clone: four cases (since
    parked) carrying `pytestmark = pytest.mark.requires_ollama`, marked
    correctly, still hit `NetworkAccessRefused`. They arrive as ERRORS rather than
    failures, which is the tell: it is fixture SETUP that refuses, not a test
    body.

    The cause is fixture ordering. pytest instantiates higher-scoped fixtures
    FIRST, and that file resolves its embedder in two `scope="module"` fixtures.
    An autouse function-scoped fixture has not run when they do, so the flag it
    flips is still closed and the marker that would have opened it has not been
    read. A per-test switch cannot cover work that happens before the per-test
    phase begins.

    `pytest_runtest_protocol` brackets the ENTIRE protocol for an item -- setup
    (including every higher-scoped fixture instantiated on its behalf), call,
    and teardown -- so a marked test's module fixture is inside the window. The
    same window is why the restore in the `finally` matters here in a way it
    did not before: a module fixture is torn down during the last item of its
    module, and that item is the one whose markers opened the gate.

    This was visible only where the daemon is REACHABLE. In a worktree with no
    pin the module fixtures skip before they open a socket, so the same code
    reported four honest skips and the defect could not appear. Green here and
    red in HELM meant different visibility, not a regression -- the second time
    that happened in one evening.
    """
    global _NETWORK_ALLOWED, _OLLAMA_ALLOWED
    previous = _NETWORK_ALLOWED, _OLLAMA_ALLOWED
    wants_ollama = any(
        item.get_closest_marker(name) is not None for name in _OLLAMA_MARKERS
    )
    _NETWORK_ALLOWED = any(
        item.get_closest_marker(name) is not None for name in _NETWORK_MARKERS
    )
    _OLLAMA_ALLOWED = wants_ollama

    # THE INHERITED EMBEDDING PIN is contained in the SAME window, and for the
    # same reason. It lived in an autouse function-scoped fixture until
    # 2026-09-04, so a module-scoped fixture resolving
    # `embeddings.index_embed_preference` read the ambient value before the
    # containment existed.
    #
    # Third ring of the containment `_mute_telegram_targets` above performs for
    # the notification targets: a name in the ambient environment silently
    # decides a verdict. `HEADING_OS_INFERENCE_EMBED_HOST` is source 2 of that
    # resolution, so a shell that exports it repoints every embed caller in the
    # suite at a host the test never named. MEASURED 2026-09-03: a test (since
    # parked) that wrote its own config into a `tmp_path` precisely to control
    # this passed with the name unset and FAILED with it set, on its very first
    # assertion: the environment reached around it.
    #
    # Blanked rather than deleted: every reader uses
    # `os.environ.get(name, "")`, so an empty value is the documented no-op,
    # while a deleted name is recreated by the first `load_env()` any test
    # performs. A test that genuinely needs the operator's pin marks itself
    # `requires_ollama` and keeps it -- the same switch that opens the gateway
    # leg of the egress guard.
    # TWO names since the 2026-09-24 rename: the live one, and what it was
    # called before. The old one is blanked because `env_override` REFUSES when
    # it is set, so a shell that still exports it would turn every embed test
    # into a refusal about that shell rather than about the tree.
    pins = ("HEADING_OS_INFERENCE_EMBED_HOST", "HEADING_OS_OLLAMA_EMBED_HOST")
    pin_was = {name: os.environ.get(name) for name in pins}
    if not wants_ollama:
        os.environ.update(dict.fromkeys(pins, ""))
    try:
        yield
    finally:
        _NETWORK_ALLOWED, _OLLAMA_ALLOWED = previous
        if not wants_ollama:
            for name, was in pin_was.items():
                if was is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = was


# ============================================================
# A test that moves a pinned name fails ITSELF, not the test that reads it
# ============================================================
#
# Everything above pins or blanks a name in `os.environ` and relies on the value
# STAYING put for the rest of the worker's session. Nothing checked that it did.
# MEASURED 2026-09-13 in a full pre-push run: three containment tests read the
# real timezone and live Healthchecks names instead of the pins, and all three
# passed alone. A module imported a package at collection whose `__init__` ran
# `dotenv.load_dotenv(override=True)`; `.venv` sits inside the engine tree, so
# the engine `.env` was found from any working directory and every xdist worker
# had its environment rewritten before its first test.
#
# So the names are snapshotted before every test and before every collector and
# compared after. A drift fails the item, or errors the collector, BY NAME,
# listing the names that moved and never their values (one is a deadman URL,
# another the bot token), and then restores the snapshot: the leak is contained
# in the same breath it is reported, so nothing later in the worker can reach
# hc-ping.com through it.
#
# What is watched: HEADING_OS_TZ and every name this file pins or blanks; every
# `*_HC_*` name and every name whose VALUE is a ping URL, by the same rule the
# containment above matches on; every `*_TELEGRAM_TARGET` and the bot token. The
# set is re-derived at each snapshot, so a name that APPEARS during a test is
# caught as an appearance. That is the leak `_mute_telegram_targets` documents:
# `monkeypatch.delenv(name, raising=False)` on an absent name gets no undo
# entry, and the next `load_env()` creates the name for good.
#
# Two hooks rather than an autouse fixture, for the reason
# `pytest_runtest_protocol` gives above: a module-scoped fixture runs before any
# function-scoped one. `pytest_runtest_setup` with `tryfirst` snapshots before
# any fixture; `pytest_runtest_teardown` with `trylast` compares after every
# finalizer, monkeypatch's undo included, so a test that used monkeypatch leaves
# no trace and a test that wrote `os.environ` directly does. A module fixture's
# own leak lands on the last item of its module.
#
# A collection-time drift is a collection ERROR on the module, after which pytest
# stops the session ("Interrupted: 1 error during collection") unless
# `--continue-on-collection-errors` is passed. Deliberate: a module that rewrites
# the process environment at IMPORT has reached every worker before a single
# test ran, and there is no item to fail in its place. The cure is an import that
# keeps the environment authoritative, called from a fixture and never at
# module level. The one package known to do this left the engine on 2026-10-04
# (HOS-198); the guard stays, because any dependency that calls
# `load_dotenv(override=True)` at import does the same thing.

_PINNED_EXACT = (
    "HEADING_OS_TZ",
    "HEADING_OS_RUNTIME_REDIRECT",
    "WS_RATE_LIMIT_STATE",
    "HEADING_OS_STATE_DIR",
    "WS_OVERLAY_WATCH_OWNER",
    "HEADING_OS_INFERENCE_EMBED_HOST",
    "TELEGRAM_NOTIFY_BOT_TOKEN",
)
# NOT watched: the venv re-exec sentinel `setdefault`ed at the top of this file.
# `ensure_venv()` POPS it on its first call in a process and remembers the answer
# in `_SENTINEL_SEEN`, by design, so that descendants do not inherit the flag.
# MEASURED 2026-09-13 with it on this list: 99 tests and 32 collected modules
# were named for a name that is meant to move, and the 32 collection errors
# turned every child pytest a test spawns into "Interrupted: error during
# collection", which took `tests/security` and the whole-suite collection
# checks down with them.
_PINNED_KEY = pytest.StashKey[dict]()
_PING_HOST = "hc-ping.com"


def _pinned_names() -> set[str]:
    """The watched names, re-derived from the live environment each time."""
    names = set(_PINNED_EXACT) | set(_MUTED_TELEGRAM)
    for name, value in os.environ.items():
        if ("_HC_" in name or name.endswith("_TELEGRAM_TARGET")
                or _PING_HOST in value):
            names.add(name)
    return names


def _pinned_snapshot(names=None) -> dict:
    """Name -> value, with `None` for absent, so an appearance counts as a move."""
    return {name: os.environ.get(name) for name in (names or _pinned_names())}


def _pinned_drift(before: dict) -> list:
    """The names whose value now differs from `before`, appearances included."""
    after = _pinned_snapshot(set(before) | _pinned_names())
    return sorted(name for name, value in after.items()
                  if value != before.get(name))


def _restore_pinned(before: dict, drifted: list) -> None:
    for name in drifted:
        value = before.get(name)
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def _pinned_drift_message(who: str, phase: str, drifted: list) -> str:
    return (
        f"{who} left the pinned environment changed during {phase}: {drifted}. "
        "The next test in this worker to read one of these would have failed in "
        "its place. The values were restored here and are deliberately not "
        "printed. Set a name with monkeypatch or restore it in a finally; a "
        "package that rewrites the environment at import is imported from a "
        "fixture and never at module level."
    )


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    item.stash[_PINNED_KEY] = _pinned_snapshot()


@pytest.hookimpl(trylast=True)
def pytest_runtest_teardown(item, nextitem):
    before = item.stash.get(_PINNED_KEY, None)
    if before is None:
        return
    drifted = _pinned_drift(before)
    if not drifted:
        return
    _restore_pinned(before, drifted)
    pytest.fail(_pinned_drift_message(item.nodeid, "the test", drifted),
                pytrace=False)


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    before = _pinned_snapshot()
    outcome = yield
    drifted = _pinned_drift(before)
    if not drifted:
        return
    _restore_pinned(before, drifted)
    try:
        report = outcome.get_result()
    except BaseException:  # noqa: BLE001 - the collector already failed loudly
        return
    report.outcome = "failed"
    report.longrepr = _pinned_drift_message(
        collector.nodeid or collector.name, "collection", drifted)


# ============================================================
# Model resolution is pinned, so the suite does not go red on the clock
# ============================================================
#
# MEASURED 2026-08-31: 40 tests across 10 files failed with
# `NetworkAccessRefused`, and the whole cause was a cache 18 minutes past its
# 24-hour TTL. `scripts/utils/claude_models.resolve()` asks the Anthropic Models
# API for the newest model per family and caches the answer for a day
# (`CACHE_TTL_SECONDS`). Inside the TTL every one of those tests passed; outside
# it, `fetch_from_api()` reached for the network and the egress guard refused.
#
# The suite therefore went red on wall-clock time rather than on code, which is
# the shape this workspace's memory names as "a test that reads the host clock
# is not a test". Nobody had changed anything.
#
# `fetch_from_api()` is written to degrade: it catches `HTTPError`, `URLError`,
# `OSError`, `TimeoutError` and the decode errors, returns `{}`, and `resolve()`
# then falls through to the cache and finally to `BASELINE`, which is exactly
# what a public clone with no API key gets. The ONE thing it cannot catch is
# `NetworkAccessRefused`, a `RuntimeError` this file raises.
#
# Two fixes were possible and this is the second one.
#
# Rejected: make `NetworkAccessRefused` inherit `OSError`. It would fix all 40
# at a stroke, and it would also let every `except OSError` in the tree silently
# swallow a test's attempt to leave the machine. The egress guard's whole value
# is being loud, so widening what can catch it is the wrong direction.
#
# Chosen: pin `fetch_from_api` to `{}` for the whole suite. Model
# resolution then always degrades to cache-or-BASELINE, deterministically, and a
# test that genuinely needs the live API marks itself `network` and gets the real
# function back. One seam, and the next test author cannot forget it.
_MODEL_PIN_MARKERS = _NETWORK_MARKERS


# The DATA-root pin below is UNIVERSAL since 2026-09-21; before, only two test
# packages pinned themselves and a child of any other test inherited the
# operator's real `HEADING_OS_DATA`.

#: The escape hatch, and it is a marker rather than a list of paths, so the
#: exemption sits in the test that needs it with its reason beside it. A path
#: list in this file is an exemption nobody reading the test can see.
#:
#:     @pytest.mark.live_data_root  # <why this test needs the real overlay>
#:
#: Use it only for a test whose SUBJECT is the operator's real tree. A test that
#: merely resolves a path does not need one: `tmp_path` resolves the same.
LIVE_DATA_ROOT_MARKER = "live_data_root"


@pytest.fixture(autouse=True)
def _pin_the_data_root_even_on_package_re_entry(request, tmp_path, monkeypatch):
    """Point every test's `HEADING_OS_DATA` at its own `tmp_path`.

    Here, in the root conftest, because a root conftest is loaded once and
    never re-entered: MEASURED 2026-09-01, directory conftests' autouse pins
    were DROPPED when collection left a package and re-entered it, and those
    tests then ran against the operator's live overlay. A child process
    inherits the variable, so this also keeps every spawned child off the live
    root. A test whose SUBJECT is the real tree opts out with the
    `live_data_root` marker.
    """
    node_path = Path(str(request.node.fspath)).resolve()
    try:
        node_path.relative_to(Path(__file__).resolve().parent)
    except ValueError:
        # A test collected from outside tests/ entirely. Not ours to pin.
        return
    if request.node.get_closest_marker(LIVE_DATA_ROOT_MARKER) is not None:
        # The test says its subject IS the real tree. Leave the environment
        # alone and let it answer for itself.
        return
    monkeypatch.setenv("HEADING_OS_DATA", str(tmp_path))


@pytest.fixture(scope="session")
def _scratch_data_root_parent(tmp_path_factory):
    """One numbered directory for the whole session, not one per test.

    `tmp_path_factory.mktemp` lists its base directory to number the next one,
    so asked per test it is O(n^2). A reading of the code; no wall-clock gain
    is claimed.
    """
    return tmp_path_factory.mktemp("scratch-data-roots")


@pytest.fixture
def scratch_data_root(_scratch_data_root_parent, request, monkeypatch):
    """Point `HEADING_OS_DATA` at an empty scratch tree of its own for this test.

    The universal pin above uses the test's `tmp_path`. This is for a file
    that builds a tree in `tmp_path` and asserts over it, where a data root in
    the same directory would have the code under test create `.sync/` and its
    kin inside the tree being walked. A file opts in with one line:

        pytestmark = pytest.mark.usefixtures("scratch_data_root")

    Named from a digest of the nodeid, not `hash()`, which is salted per
    process, so two workers would disagree about the name. A file whose
    SUBJECT is the data overlay must not take it.
    """
    import hashlib
    name = hashlib.sha256(
        str(request.node.nodeid).encode("utf-8", "replace")).hexdigest()[:16]
    root = _scratch_data_root_parent / name
    root.mkdir(exist_ok=True)
    monkeypatch.setenv("HEADING_OS_DATA", str(root))


@pytest.fixture(autouse=True)
def _pin_model_resolution(request):
    """No unit test resolves a model id over the network.

    The per-process memo and the failure latch are cleared on the way in AND on
    the way out. They are module globals: one test that resolved a real id would
    otherwise hand it to every later test, and a `_FETCH_FAILED` left standing
    would suppress the fetch for a `network`-marked test that wanted it.
    """
    if any(request.node.get_closest_marker(name) is not None
           for name in _MODEL_PIN_MARKERS):
        yield
        return
    try:
        from scripts.utils import claude_models
    except ImportError:
        yield
        return

    real_fetch = claude_models.fetch_from_api
    claude_models.fetch_from_api = dict
    claude_models._RESOLVED.clear()
    claude_models._FETCH_FAILED = False
    try:
        yield
    finally:
        claude_models.fetch_from_api = real_fetch
        claude_models._RESOLVED.clear()
        claude_models._FETCH_FAILED = False


# Only the OUTERMOST pytest enforces the temp-leak baseline and calls a total
# the run's. The same owner check `WS_RATE_LIMIT_STATE` needs, for the same
# measured reason: this suite spawns pytest CHILDREN and each imports this file.
# MEASURED 2026-09-03: three child pytests exited 1 on a budget written for the
# parent.
_OWNS_OVERLAY_WATCH = "WS_OVERLAY_WATCH_OWNER" not in os.environ
os.environ["WS_OVERLAY_WATCH_OWNER"] = "1"


# Reachable-child counts collected from xdist workers as each finishes. A list
# rather than an int so the hook can mutate it without a `global`, which is the
# shape that silently made a module name local and broke `arm()` earlier today.
_WORKER_REACHABLE_TOTAL = [0]

# Attribution folded up from the workers: nodeid -> [count, example command].
# The controller's OWN map is useless for this: it spawns nothing but the
# execnet bootstrap of the workers, so before this existed the report's sixteen
# examples were sixteen copies of that bootstrap under `<unknown test>` while
# the ten thousand spawns it enforced against arrived as a bare integer.
_WORKER_SPAWN_ATTRIBUTION: dict[str, list] = {}

# How many attribution rows each worker sends back. `workeroutput` is pickled
# through execnet, so this is a wire budget, not a display one: 100 rows of a
# nodeid plus a 120-char command is ~20 KB per worker. The display limit is
# separate and smaller, applied to the aggregate.
_WORKER_ATTRIBUTION_ROWS = 100

# THE DISTINCT-TEST COUNT, and why it is not `len(_WORKER_SPAWN_ATTRIBUTION)`:
# that map is sliced per worker for the wire, so its length counts the tests
# that survived the slice. This is a UNION OF DIGESTS of every spawning test, so
# `UNKNOWN_TEST` counts once rather than once per worker, and unlike a child
# COUNT it does not move with the scheduler (MEASURED 2026-09-06: nine runs of
# one tree spread +-3% on children and by two on distinct tests, once
# `_wire_safe` below stopped workers dying on the wire).
_WORKER_SPAWNER_DIGESTS: set[str] = set()

# Did any process's attribution map hit `_CHILD_SPAWN_BY_TEST_CAP`? Past the cap
# a nodeid never seen before is dropped from the map, so the digest union is a
# LOWER BOUND and the report has to say so rather than print a number that reads
# exact.
_WORKER_MAP_CAPPED = [0]


# ============================================================
# Scratch left behind in the SHARED temp directory
# ============================================================
#
# The mechanics, the measurement and the reason the two halves are weighted
# differently all live in `tests/tmp_leak_guard.py`. What lives here is the
# wiring: when each half is armed, who is allowed to judge, and what the
# controller prints.

from tests import tmp_leak_guard as _tmpguard  # noqa: E402


def _wire_safe(value):
    """The same value with every string encodable as UTF-8.

    THE ONE PLACE ANYTHING GOES INTO `workeroutput`, and it is a repair, not a
    precaution. MEASURED 2026-09-06, full suite, `-n auto`: two workers per run
    died at teardown with

        UnicodeEncodeError: 'utf-8' codec can't encode character '\\udcff'
        in position 88: surrogates not allowed

    out of `execnet.gateway_base._write_unicode_string`. Every test passed;
    the deaths happen in xdist's `workerfinished` send, AFTER the last test, so
    the run is green and only a `[gwN] node down:` line in the middle of the
    output says otherwise. The controller then has no `workeroutput` for that
    node at all, so THE WHOLE WORKER'S REPORT IS LOST -- its child count and its
    distinct-test digests together.

    Where the byte came from: two tests (since parked) deliberately built a
    repository whose DIRECTORY NAME held a non-UTF-8 byte and ran `git init`
    on it. `sys.argv` and `os.environ` carry that byte back as a surrogate
    escape, the guard records the command string verbatim as an example, and
    the example rides `overlay_reachable_by_test` onto the wire.

    WHAT THIS COST, measured the same day: which worker holds those tests moves
    with xdist's scheduling, so 0 to 3 workers' reports vanished per run and the
    count moved 100 to 400 with no change in the tree.

    `errors="replace"` rather than `surrogatepass`: the byte is data in an
    example string nobody parses, and the `?` that ENCODE-side "replace"
    substitutes is honest about what was there. `surrogatepass` would put the surrogate back on the
    wire and re-open the same hole one layer down.
    """
    if isinstance(value, str):
        return value.encode("utf-8", "replace").decode("utf-8")
    if isinstance(value, tuple):
        return tuple(_wire_safe(v) for v in value)
    if isinstance(value, list):
        return [_wire_safe(v) for v in value]
    if isinstance(value, dict):
        return {_wire_safe(k): _wire_safe(v) for k, v in value.items()}
    return value


def _send_upward(output, **values):
    """Put every value on the wire, wire-safe, in the one place that can be."""
    for key, value in values.items():
        output[key] = _wire_safe(value)

_TMP_BEFORE: set[str] = set()
_TMP_LEAK_BASELINE = _ENGINE_ROOT / "config" / "tmp-leak-baseline.json"

# The config object of the session pytest actually started, captured in
# `pytest_sessionstart`.
#
# WHY IDENTITY, AND NOT A FLAG. A test that drives `pytest_sessionfinish` with
# a SYNTHETIC session must not receive the REAL run's counters: MEASURED
# 2026-09-04, three such tests (since parked) went red because this guard
# reported the real session's leaks into their fake reporter and set their fake
# session's exitstatus. The identity check makes that impossible rather than
# unlikely.
_REAL_SESSION_CONFIG = None

# Folded up from the workers, same shape as the overlay map above:
# nodeid -> [count, one example path].
_WORKER_TMP_ATTRIBUTION: dict[str, list] = {}
_WORKER_TMP_TOTAL = [0]
_WORKER_TMP_ROWS = 100


def declared_basetemp(config) -> Path:
    """Where this run's `tmp_path` directories will be carved out of.

    NOT `_managed_temp_root`, and NOT the factory at all. `_managed_temp_root`
    below answers a different question (which tree pytest reclaims) and it gets
    there through `TempPathFactory.getbasetemp()`, which is fine for a guard
    that runs after the run has been allowed to start and is exactly wrong for
    one that decides whether it may. `getbasetemp()` is not a reader: on an
    explicit `--basetemp` it does `rm_rf(path)` and then `mkdir`, so merely
    ASKING destroys and recreates the directory. A guard whose refusal deletes
    the thing it is refusing about is the shape recorded in auto-memory as "a
    guard that deleted the data, then refused"; MEASURED here on 2026-09-08,
    the first draft of this function left an empty `.tmp/guard-probe/` behind
    in the work tree on every refusal.

    So the OPTION is read instead, which is inert. A relative value is resolved
    against the current directory, which is what pytest does with it a moment
    later (`Path(config.option.basetemp)` with no anchor). With no `--basetemp`
    the factory would build one under the system temp directory, so that is
    what is returned; it is only ever inside a checkout if `TMPDIR` is, and
    that case is caught by the same comparison rather than by a special case.
    """
    given = getattr(getattr(config, "option", None), "basetemp", None)
    if given:
        return Path(given).expanduser().resolve()
    return Path(tempfile.gettempdir()).resolve()


def _refuse_a_basetemp_inside_the_checkout(config) -> None:
    """Refuse the whole session when `tmp_path` would land in this work tree.

    MEASURED 2026-09-08, in the operator's live clone. An unattended agent ran

        pytest tests/ -q -x -n auto --basetemp=.tmp/night-repair/pytest-bt

    from the engine root. The path is RELATIVE, so every `tmp_path` in that run
    resolved INSIDE the checkout. A test that scaffolds a data overlay called
    `git_init_commit` in `scripts/create-data-repo.py`, whose idempotency probe
    asked `git rev-parse --is-inside-work-tree` -- "is this path inside ANY
    repository", read as "is this the repository I already initialised". For a
    target under the engine it answers yes, `git init` is skipped, and the three
    lines below it ran `git config`, `git add -A` and `git commit` against THE
    ENGINE. The result was commit `ed7cee1` on `main`: six files, two unrelated
    sessions' work, under a message describing neither.

    WHAT LIMITED THE DAMAGE WAS LUCK. The scaffolded fake overlay landed under
    `.tmp/`, which is gitignored, so it contributed nothing to that commit. A
    basetemp anywhere else in the tree would have committed a fabricated data
    overlay -- a `.gitignore`, a README, `crm/contacts/`, `context/` -- into the
    PUBLIC engine repository. `.tmp/` being gitignored is not protection; it is
    where that particular run happened to point.

    HERE, rather than at each of the four sites that can reach
    `git_init_commit` with a target of their own. One place covers all four and
    every future one, and it turns a mistake that is merely NOTICEABLE into one
    that is IMPOSSIBLE: the run never starts, so no test can be the one that
    fires.

    A `UsageError` at session start, not a skip per test. A per-test skip is a
    wall of noise and half the suite would still run against a poisoned tree;
    pytest prints a `UsageError`'s message on stderr and exits 4 with nothing
    collected.

    The default basetemp (under the system temp directory) and an explicit one
    OUTSIDE the clone both pass untouched, including under `-n auto`, where a
    worker's basetemp is a `popen-gwN` subdirectory of the session's. Compared
    with `is_relative_to` on two resolved paths, never a string prefix: a
    sibling checkout named `<clone>-2` shares the prefix and contains nothing.
    """
    base = declared_basetemp(config)
    if not base.is_relative_to(_ENGINE_ROOT):
        return
    raise pytest.UsageError(
        f"REFUSING to run: pytest's basetemp resolves to {base}, which is "
        f"inside this checkout ({_ENGINE_ROOT}). Every tmp_path in this run "
        f"would be a directory in the engine work tree, so any test that runs "
        f"`git init`, `git add -A` or `git commit` against its own scratch "
        f"directory operates on THIS REPOSITORY instead. That happened on "
        f"2026-09-08 and produced commit ed7cee1 on main. Being under a "
        f"gitignored path such as .tmp/ is NOT protection: it decides only "
        f"whether the fabricated files are also committed. Pass an absolute "
        f"--basetemp outside every git checkout, or drop the flag and let "
        f"pytest use the system temp directory."
    )


def _managed_temp_root(config) -> Path:
    """The tree pytest reclaims on its own, which this guard does not police.

    NOT `getbasetemp()`, which is this session's numbered directory (and, in an
    xdist worker, a `popen-gwN` inside it). The retention that matters is
    `make_numbered_dir_with_cleanup(keep=3)` and it prunes one level ABOVE the
    numbered directory, so the whole of `/tmp/pytest-of-<user>` dies on pytest's
    schedule and nothing under it is a leak. Walking up to the child of the
    shared temp directory finds that level from either depth without hardcoding
    either one.

    An explicit `--basetemp` can point anywhere, including outside the shared
    temp directory. The walk then terminates without a match and the basetemp
    itself is returned, which is the right answer: the caller named that
    directory and owns it.
    """
    factory = getattr(config, "_tmp_path_factory", None)
    if factory is None:
        # No temp factory means no managed tree; treat everything as unmanaged
        # rather than silently exempting the shared root itself.
        return Path(tempfile.gettempdir()) / "pytest-no-basetemp"
    base = Path(factory.getbasetemp()).resolve()
    shared = Path(tempfile.gettempdir()).resolve()
    node = base
    while node.parent != node:
        if node.parent == shared:
            return node
        node = node.parent
    return base


def _tmp_leak_baseline() -> int:
    """The frozen count of surviving unmanaged entries, or 0 when unreadable.

    Fails toward STRICT: a deleted baseline must go red, not quiet.
    """
    import json
    try:
        data = json.loads(_TMP_LEAK_BASELINE.read_text(encoding="utf-8"))
        return int(data["surviving_unmanaged_entries"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def pytest_runtest_logstart(nodeid, location):
    """Name the test that is about to run, so an allocation can be attributed.

    `logstart` rather than `setup`: it fires before the setup phase, so a
    fixture that allocates is attributed to the test that requested it rather
    than to whatever ran previously.
    """
    _tmpguard.CURRENT_TEST = nodeid


def _tmp_leak_sessionfinish(session):
    """Report both halves; enforce only the attributable one.

    Collect-only sessions are exempt for the same reason the overlay watch
    exempts them: they run no test body, so they cannot be the writer, and their
    sub-second window lands on whatever else the machine did in that second.
    """
    # A synthetic session belongs to whichever test built it; see
    # `_REAL_SESSION_CONFIG` above for what this prevents and the measurement
    # behind it.
    if session.config is not _REAL_SESSION_CONFIG:
        return
    if session.config.getoption("collectonly", False):
        return

    mine = _tmpguard.survivors()
    worker = hasattr(session.config, "workerinput")
    if worker:
        # A worker reports upward and judges nothing. Without this the gate
        # would bind only to single-process runs, and the suite is run under
        # `-n auto`; a guard that only fires where nobody looks is decoration.
        output = getattr(session.config, "workeroutput", None)
        if output is not None:
            # Through `_send_upward`, not by assignment: a leaked scratch path
            # can hold the same undecodable byte the overlay example does, and
            # one unencodable string anywhere in this dict kills the whole
            # report. See `_wire_safe`.
            _send_upward(
                output,
                tmp_leak_survivors=len(mine),
                tmp_leak_by_test=_tmpguard.survivors_by_test(_WORKER_TMP_ROWS))
        return

    total = len(mine) + _WORKER_TMP_TOTAL[0]
    attribution = {who: [count, example] for who, count, example
                   in _tmpguard.survivors_by_test(_WORKER_TMP_ROWS)}
    _tmpguard.merge_rows(
        [(who, count, example) for who, (count, example)
         in _WORKER_TMP_ATTRIBUTION.items()],
        into=attribution)

    # THE SAME AGGREGATION TRAP the overlay ratchet hit on 2026-09-03: under
    # `-n auto` the controller runs no test bodies, so its own count is 0 and a
    # gate enforcing against 0 would pass every sharded run while printing a
    # number. A sharded run whose workers reported nothing says so and enforces
    # nothing.
    sharded = bool(session.config.getoption("numprocesses", None))
    aggregation_lost = sharded and _WORKER_TMP_TOTAL[0] == 0
    baseline = _tmp_leak_baseline()
    enforced = _OWNS_OVERLAY_WATCH and not aggregation_lost
    over = enforced and total > baseline

    # THE DIFF NEVER SPEAKS ON ITS OWN, and this is a correction, not a taste
    # call. The first draft printed whenever the shared temp directory had
    # gained an entry, which on this machine is every run: two other worktrees,
    # the main clone, the daemons and every browser write there. MEASURED
    # 2026-09-04 over a full run, 24 entries appeared of which 18 were nobody's
    # business of this suite's. That turned a gate into a line printed every
    # time, and a report that always prints is not a gate.
    #
    # So the attributable half decides whether anything is said, and the diff is
    # printed only BESIDE a finding, as the context that can see a child
    # process's scratch. A quiet run stays silent.
    if not over and not mine and not _WORKER_TMP_TOTAL[0]:
        return
    observed = _tmpguard.appeared_and_survived(_TMP_BEFORE,
                                               _tmpguard.top_level_snapshot())

    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        if over:
            session.exitstatus = 1
        return

    established = [
        f"{total} entry/entries created by THIS run outside pytest's managed "
        f"tree and still on disk (frozen baseline {baseline}"
        + ("" if enforced else ", not enforced: " + (
            "sharded run and no worker total arrived, so this number is NOT "
            "the run's" if aggregation_lost else "nested pytest"))
        + ")",
    ]
    rows = sorted(attribution.items(), key=lambda kv: (-kv[1][0], kv[0]))[:10]
    if rows:
        established.append(
            # Same shape as the overlay watch's offender line, and the same
            # correction: `example` is one entry out of `count`, not all of
            # them. See the note beside `top spawners:` below.
            "by test: " + "; ".join(f"{count}x {who} (e.g. {example})"
                                    for who, (count, example) in rows))

    reporter.write_line("")
    label = "ERROR" if over else "NOTE"
    reporter.write_line(f"{label}: temp-directory leak. " + " | ".join(established),
                        red=over, yellow=not over)
    if observed:
        reporter.write_line(
            f"ALSO OBSERVED, and NOT established as this run's: {len(observed)} "
            f"top-level entry/entries appeared in {tempfile.gettempdir()} during "
            f"this session and are still there ("
            + ", ".join(_tmpguard.summarise(observed)[:8])
            + "). Every other process on this machine writes there too, so this "
            "is an upper bound, never an accusation of a test. It is the only "
            "half that can see a CHILD process's scratch, which is why it is "
            "printed rather than dropped.", yellow=True)
    if over:
        first = (f" Start with {rows[0][0]} ({rows[0][1][0]} entry/entries)."
                 if rows else " No test could be named.")
        reporter.write_line(
            "More scratch survived outside pytest's managed tree than the "
            f"frozen {baseline}. Pass `dir=` pointing at a `tmp_path`, or take "
            "the directory from `tmp_path_factory`, in the named test."
            + first, red=True)
        session.exitstatus = 1


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error):
    """Sum each worker's reachable-child count into the controller's total.

    `optionalhook=True` is not decoration. This hook belongs to xdist, and a
    run started with `-p no:xdist` has no such hookspec: pluggy then rejects
    the whole conftest with `PluginValidationError: unknown hook`, which
    surfaces as INTERNALERROR and takes the session down. MEASURED 2026-09-03 —
    and it also explains why the first aggregation attempt reported 0 under
    `-n auto`: the hook was never registered at all, so nothing was ever
    summed.

    `workeroutput` is xdist's own channel for exactly this. Without it the
    controller sees only its own process, which spawns no test children, and
    the total it enforces against is zero regardless of what the run did.
    """
    output = getattr(node, "workeroutput", None) or {}
    _WORKER_REACHABLE_TOTAL[0] += int(output.get("overlay_reachable", 0) or 0)
    # The count alone told an operator that the gate had tripped and nothing
    # about what to do next. The rows do.
    _guard.merge_spawn_attribution(
        output.get("overlay_reachable_by_test") or (),
        unattributed=int(output.get("overlay_reachable_unattributed", 0) or 0),
        into=_WORKER_SPAWN_ATTRIBUTION)
    # The distinct-test measure rides the same message. A UNION, so a worker
    # that reports nothing costs nothing and a nodeid seen twice counts once.
    _WORKER_SPAWNER_DIGESTS.update(
        output.get("overlay_reachable_test_digests") or ())
    _WORKER_MAP_CAPPED[0] += int(output.get("overlay_reachable_map_capped", 0) or 0)
    # Same fold, for the temp-directory guard. It rides this hook rather than
    # adding a second one: xdist gives each node exactly one teardown, and two
    # implementations of `pytest_testnodedown` in one conftest is a shape that
    # silently drops one of them.
    _WORKER_TMP_TOTAL[0] += int(output.get("tmp_leak_survivors", 0) or 0)
    _tmpguard.merge_rows(output.get("tmp_leak_by_test") or (),
                         into=_WORKER_TMP_ATTRIBUTION)


def pytest_sessionstart(session):
    global _RESTORE_SOCKET_GUARD, _TMP_BEFORE, _REAL_SESSION_CONFIG
    # FIRST, before anything else is armed or snapshotted. A run whose scratch
    # lands in the work tree must not reach collection at all; see the function
    # for the incident it refuses.
    _refuse_a_basetemp_inside_the_checkout(session.config)
    _RESTORE_SOCKET_GUARD = _install_socket_guard()
    _REAL_SESSION_CONFIG = session.config
    # Both halves of the temp-directory guard, armed before any test body runs.
    # The snapshot has to be first, or every entry this session's own setup
    # creates reads as pre-existing.
    _TMP_BEFORE = _tmpguard.top_level_snapshot()
    _tmpguard.arm(_managed_temp_root(session.config))
    # MODE_REFUSE explicitly, never the environment's answer. `arm()` defaults to
    # `resolve_mode()`, which honours `HEADING_OS_OVERLAY_GUARD=record`, and a
    # test run must not be softened to logging by a variable someone exported for
    # a measurement and forgot. A suite that records instead of refusing is a
    # suite whose isolation failures all pass.
    _guard.arm(_guard.MODE_REFUSE)


def pytest_sessionfinish(session, exitstatus):
    # First, and outside the overlay watch's own early returns: the temp-leak
    # guard is armed unconditionally in `pytest_sessionstart`, so gating it on
    # `_WATCH_BEFORE` would make it dormant in exactly the runs where the
    # overlay watch happens not to be.
    _tmp_leak_sessionfinish(session)
    if not _guard._WATCH_BEFORE:
        return
    # A collect-only session runs no test body, so it cannot be the writer, and
    # its sub-second window lands on whatever else touches the overlay in that
    # second (MEASURED 2026-09-02: a compaction hook's write failed a child
    # collect-only run; that child is gone since 2026-09-21, the race is not).
    if session.config.getoption("collectonly", False):
        return
    # NOT `if not complaints: return`: the count below is about this run's own
    # children, which a diff of the tree cannot see.
    complaints = _guard.watch_complaints(_guard._WATCH_BEFORE, _guard._watch_snapshot())
    # A REPORT, never a gate: nothing here touches `session.exitstatus`. The
    # diff cannot name a writer, because daemons write the live overlay on their
    # own schedule (MEASURED 2026-09-03: a sync daemon's run rewrote the files
    # the guard named, 0.0007 s apart). `_CHILD_SPAWNS` is a suspect list, never
    # an accusation. The child count moves with the scheduler (MEASURED
    # 2026-09-06: +-3% over nine runs of one tree), so its ceiling was retired
    # 2026-09-21 and its baseline comparison 2026-10-16. The exposure is held at
    # the source instead: the universal DATA-root pin above, and
    # `scripts/check-live-data-root.py` over the `live_data_root` markers.
    # No filename exclusions: a list of files daemons touch goes quiet the
    # first time a new daemon writes something.
    reachable = _guard._CHILD_SPAWN_COUNT
    # A SHARDED RUN'S NUMBER IS NOT THE RUN'S. `_CHILD_SPAWN_COUNT` is per
    # PROCESS, so under `-n auto` each worker sees a shard (MEASURED 2026-09-03,
    # the same tree twice: `-n auto` reported 16, a single-process run over a
    # subset 654). The total is the run's only where one process sees
    # everything; a sharded run still REPORTS its shard, labelled as one.
    worker = hasattr(session.config, "workerinput")
    if worker:
        # A worker cannot judge, but it can REPORT UPWARD. Without this the
        # report covers only single-process runs, and MEASURED 2026-09-03 a
        # serial full run takes 37 minutes against 8 under `-n auto`.
        output = getattr(session.config, "workeroutput", None)
        if output is not None:
            # ATTRIBUTION, not just the total. Bounded on purpose: this is
            # pickled back through execnet, so the whole map is not sent and
            # the top slice is. Whatever the slice leaves behind is reported as
            # a number in the same breath, so the controller never presents a
            # partial list as the run.
            rows = _guard.top_spawners(_WORKER_ATTRIBUTION_ROWS)
            # EVERY value through `_send_upward`. One unencodable string in this
            # dict does not lose one field, it loses the entire worker: execnet
            # raises inside the `workerfinished` send and the node dies with no
            # `workeroutput` at all. Measured: `_wire_safe`.
            _send_upward(
                output,
                overlay_reachable=reachable,
                overlay_reachable_by_test=rows,
                overlay_reachable_unattributed=max(
                    0, reachable - sum(count for _, _, count in rows)),
                # The WHOLE map's key set, digested, not a slice of it. The rows
                # above are a report and are sliced for the wire; this is the
                # measure, and a sliced measure is the "baseline frozen from a
                # truncated view" trap wearing a different name. Sorted for a
                # deterministic wire image; the controller takes a set union.
                overlay_reachable_test_digests=sorted(_guard.spawner_digests()),
                overlay_reachable_map_capped=int(
                    len(_guard._CHILD_SPAWNS_BY_TEST)
                    >= _guard._CHILD_SPAWN_BY_TEST_CAP))
    else:
        reachable += _WORKER_REACHABLE_TOTAL[0]

    # DID THE AGGREGATION ACTUALLY ARRIVE? MEASURED 2026-09-03: it did not.
    # Under `-n auto` the controller reported 0 while a serial run over the same
    # tree counted 9460, because `pytest_testnodedown` and the controller's
    # `pytest_sessionfinish` do not order the way this assumed. A total of 0
    # would read as a measurement, so a sharded run whose workers reported
    # nothing SAYS SO rather than printing a figure it cannot stand behind.
    controller_sharded = (not worker) and bool(
        session.config.getoption("numprocesses", None))
    aggregation_lost = controller_sharded and _WORKER_REACHABLE_TOTAL[0] == 0

    # Whether the number below is the WHOLE RUN's, or one worker's shard, or a
    # controller total that never arrived.
    whole_run = _OWNS_OVERLAY_WATCH and not worker and not aggregation_lost

    # THE SECOND MEASURE: how many DISTINCT tests spawned such a child.
    #
    # Same invariant, a metric that does not move with the scheduler. See
    # `_WORKER_SPAWNER_DIGESTS` for the nine HELM runs that made this necessary.
    # The controller unions the workers' digests with its OWN: the execnet
    # bootstrap of each worker is a real spawn made by the controller and it is
    # already inside `reachable`, so dropping it here would make the two numbers
    # describe different runs.
    distinct_digests = set(_guard.spawner_digests())
    if not worker:
        distinct_digests |= _WORKER_SPAWNER_DIGESTS
    distinct_tests = len(distinct_digests)
    map_capped = bool(_WORKER_MAP_CAPPED[0]) or (
        len(_guard._CHILD_SPAWNS_BY_TEST) >= _guard._CHILD_SPAWN_BY_TEST_CAP)

    # THE TWO REPORT LINES: their leading wording is kept verbatim, because
    # `scripts/overlay-ratchet.py` reads a run's figures back with two regexes.
    established = [
        f"{len(complaints)} observation(s) about the overlay tree: "
        + "; ".join(complaints),
        f"{reachable} child process(es) of this run had the operator's live "
        f"data root reachable"
        + ("" if whole_run else " | NOT the run's total: " + (
            "sharded run, this is one worker's shard reported upward" if worker
            else "sharded run and no worker total arrived" if aggregation_lost
            else "nested pytest")),
        f"{distinct_tests} distinct test(s) spawned at least one such child"
        + (", a LOWER BOUND: an attribution map hit its "
           f"{_guard._CHILD_SPAWN_BY_TEST_CAP}-test cap" if map_capped else ""),
    ]
    # TOP OFFENDERS, and this is what makes the number actionable.
    #
    # Under `-n auto` the controller's own `_CHILD_SPAWNS` holds only the
    # execnet bootstrap of the workers, so the aggregate folded up through
    # `pytest_testnodedown` is the real answer and takes precedence. A
    # single-process run has no workers and its own map IS the run.
    #
    # Both are folded, never one chosen. The controller's own spawns are real
    # spawns and they are counted in `reachable`; leaving them out of the map
    # made "4 of 6 attributed" read as a slice cap when the two missing were
    # simply in the other dictionary. A copy, because this is a report and it
    # must not mutate the aggregate it reports on.
    attribution = {n: list(e) for n, e in _WORKER_SPAWN_ATTRIBUTION.items()}
    _guard.merge_spawn_attribution(
        [(n, e[1], e[0]) for n, e in _guard._CHILD_SPAWNS_BY_TEST.items()],
        into=attribution)
    offenders = _guard.top_spawners(10, source=attribution)
    if offenders:
        named = sum(count for _, _, count in offenders)
        established.append(
            "top spawners: "
            # `e.g.`, never `->`. The command is the FIRST one recorded under
            # that nodeid (`overlay_write_guard` stores `head` only when the
            # key is created) while the count is every spawn under it, so the
            # arrow asserted a pairing the data does not carry. MEASURED
            # 2026-09-15: the `<unknown test>` bucket printed
            # `224x <unknown test> -> uname -p` and two investigations went
            # hunting for 224 `uname` spawns. There are at most one per xdist
            # worker: the stdlib caches it per process, and xdist calls
            # `platform.platform()` once per worker at sessionstart
            # (`xdist/remote.py:386`). The other 200-odd spawns in that bucket
            # are different commands entirely, and the `examples:` line two
            # lines down was already printing several of them.
            + "; ".join(f"{count}x {nodeid} (e.g. {cmd})"
                        for nodeid, cmd, count in offenders)
            + f" ({named} of {reachable} attributed to the {len(offenders)} "
              f"shown; {len(attribution)} distinct test(s) reported, "
              f"{_guard._CHILD_SPAWN_UNATTRIBUTED} spawn(s) past the "
              f"per-worker slice or the map cap and so unattributed)"
        )
    spawns = _guard._CHILD_SPAWNS
    if spawns:
        shown = spawns[:10]
        established.append(
            "examples: "
            + "; ".join(f"{nodeid} -> {cmd}" for nodeid, cmd in shown)
            + ("" if len(spawns) <= 10 else f" (+{len(spawns) - 10} more kept)")
        )

    not_established = (
        "NOT established: which process performed those writes. This is a "
        "before/after diff of a live tree, and daemons write it on their own "
        "schedule. The half that CAN name a culprit is the in-process wrapper, "
        "which refuses at the moment of the write and puts the test in the "
        "traceback; it did not fire, so no test in this interpreter wrote there."
    )

    # `HEADING_OS_OVERLAY_REPORT=1` forces the NOTE on a quiet run, so the
    # counts above can be read; it only ever adds a line. Scoped to the real
    # session by identity, for the reason at `_REAL_SESSION_CONFIG`.
    forced = (bool(os.environ.get("HEADING_OS_OVERLAY_REPORT"))
              and session.config is _REAL_SESSION_CONFIG)
    if not complaints and not forced:
        return   # nothing observed and nothing asked for: say nothing

    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line("")
        # ALWAYS a NOTE, never an ERROR, and never red. This hook has set no
        # exit status since 2026-09-21; there is deliberately no branch left
        # here that could, because the way a retired gate comes back is
        # somebody restoring one condition and not the sentence beside it.
        reporter.write_line("NOTE: overlay watch. " + " | ".join(established),
                            yellow=True)
        reporter.write_line(not_established, yellow=True)
        if offenders:
            reporter.write_line(
                f"The largest single spawner is {offenders[0][0]} "
                f"({offenders[0][2]} spawn(s)). To reduce it, pass "
                f"HEADING_OS_DATA pointing at a tmp_path in the spawning "
                f"test's subprocess env.", yellow=True)

