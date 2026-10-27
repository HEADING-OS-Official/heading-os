#!/usr/bin/env python3
"""Runtime path detection for the Inbox Pulse daemon.

Centralises VM-vs-laptop detection so every daemon module imports one
function rather than re-implementing the INBOX_PULSE_STATE_DIR pattern.

The workspace root is found by walking parent directories up to the dir that
contains both config/ and scripts/, so it resolves correctly whether the code
runs on the laptop checkout or on the always-on service host - no host-specific
path literal is embedded.

Tests: none since the 2026-09-21 test-suite split. The resolution order below is
the contract; nothing now fails when a step of it is reordered or dropped.

Resolution order for get_state_dir() and get_log_dir():
  1. INBOX_PULSE_STATE_DIR env var (test/dev override; on the service host the
     unit sets it, and both answer that one directory, as they always did)
  2. the runtime store under the data root, `scripts/utils/runtime_dir.py`:
     <data_root>/runtime-junk/inbox-pulse/state/ for the heartbeat, the EWS
     cursor and the cost ledger; <data_root>/runtime-junk/inbox-pulse/logs/
     for the daily `log-<date>.jsonl`. Until 2026-09-14 everything sat in
     <data_root>/state/email-triage/, which the overlay's `/state/` rule
     ignored. The store's `state/` and `logs/` kinds are TRACKED (HELM
     ruling, 2026-09-14: a cursor and a classification trail are durable
     operational state), so on the laptop the heartbeat, the cursor, the cost
     ledger and the daily logs enter the overlay's history from the first
     commit after the move; only `.health-check.tmp` (`*.tmp`) stays ignored.

Usage::

    from scripts.inbox_pulse.paths import get_log_dir, get_state_dir, get_workspace_root

    state = get_state_dir()          # Path, created by the helper
    logs  = get_log_dir()            # Path, created by the helper
    root  = get_workspace_root()     # Path to the workspace root
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Make the foundation path helpers importable when this module is loaded
# directly (e.g. by the daemon) without the workspace root on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.utils.paths import get_data_root  # noqa: E402,F401 - re-exported
from scripts.utils.paths import (  # noqa: E402
    get_workspace_root as _shared_workspace_root,
)
from scripts.utils.data_path import data_write_dir  # noqa: E402

__all__ = ["get_workspace_root", "get_state_dir", "get_log_dir", "get_data_root",
           "get_tmp_dir"]

_THIS_FILE = Path(__file__).resolve()


# ---------------------------------------------------------------------------
# Module-level cache (path is stable for the process lifetime)
# ---------------------------------------------------------------------------

_state_dir_cache: Path | None = None
# The env value the cached state dir was resolved FROM. The cache used to key
# on nothing, so the first call in a process fixed the answer for its lifetime
# and INBOX_PULSE_STATE_DIR -- documented one line below as a "test/dev
# override" -- stopped overriding anything once any other caller had resolved
# first. A cache that ignores its own input is not a cache of that input.
_state_dir_cache_key: str | None = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_workspace_root() -> Path:
    """Return the workspace root directory.

    Delegates to `scripts.utils.paths.get_workspace_root`, which is the one
    implementation. This module used to carry a private copy that walked up
    from its own file looking for a directory holding both `config/` and
    `scripts/`. That copy answered correctly on the laptop and on the service
    host, so nothing ever looked at it again, and it silently ignored the
    `WORKSPACE_ROOT` environment override the shared helper honours.

    MEASURED 2026-08-29 with `WORKSPACE_ROOT=/tmp/pretend-workspace` exported
    and that directory seeded with `config/` and `scripts/`:

        scripts.utils.paths        -> /tmp/pretend-workspace
        scripts.inbox_pulse.paths  -> the real checkout

    Two answers to one question, and the daemon read the one that cannot be
    redirected. The caching also moved to the shared helper, which resolves in
    microseconds and needs none.
    """
    return _shared_workspace_root()


def get_state_dir() -> Path:
    """Return the state directory for daemon files (heartbeat, cursor, cost tracker).

    Resolution order (highest priority first):
      1. INBOX_PULSE_STATE_DIR env var (test/dev override)
      2. <data_root>/runtime-junk/inbox-pulse/state/ via
         `data_write_dir("runtime:inbox-pulse/state")`, which creates it and
         refuses (`DataPathError`) on a clone with no private overlay rather
         than resolving inside the engine tree.

    The env override is created here (parents=True, exist_ok=True) so callers
    never have to; the store branch is created by `data_write_dir`. Cached against
    the env value it was resolved from, so a changed override re-resolves
    instead of returning the previous answer.
    """
    global _state_dir_cache, _state_dir_cache_key

    env_override = os.environ.get("INBOX_PULSE_STATE_DIR", "").strip()
    if _state_dir_cache is not None and _state_dir_cache_key == env_override:
        return _state_dir_cache

    if env_override:
        path = Path(env_override)
        path.mkdir(parents=True, exist_ok=True)
    else:
        path = data_write_dir("runtime:inbox-pulse/state")

    _state_dir_cache = path
    _state_dir_cache_key = env_override
    return _state_dir_cache


def get_log_dir() -> Path:
    """Where the daily `log-<date>.jsonl` classification log goes.

    With INBOX_PULSE_STATE_DIR set this IS the state dir, so the service host
    keeps one directory and nothing there changes. Without it, the store's
    `logs` kind: <data_root>/runtime-junk/inbox-pulse/logs/. Not cached: the
    helper resolves in microseconds and a cache here would need the same
    keyed invalidation `get_state_dir` carries, for nothing.
    """
    if os.environ.get("INBOX_PULSE_STATE_DIR", "").strip():
        return get_state_dir()
    return data_write_dir("runtime:inbox-pulse/logs")


def get_tmp_dir() -> Path:
    """Where the opt-in `debug-trace.jsonl` goes: the store's `tmp` kind.

    The trace holds raw arguments and returns, which for this daemon means
    e-mail bodies. `state/` and `logs/` are TRACKED by the overlay (HELM,
    2026-09-14), and a debugging aid that copies mail into git history is not
    durable state; `tmp/` is ignored. With INBOX_PULSE_STATE_DIR set it stays
    beside the state, as on the service host.
    """
    if os.environ.get("INBOX_PULSE_STATE_DIR", "").strip():
        return get_state_dir()
    return data_write_dir("runtime:inbox-pulse/tmp")
