"""Paths to the email-intelligence state, and the crm-logged dedupe log.

`scripts/email-intelligence.py` writes `_latest-fetch.json` and `state.json`;
its readers (the CRM-log finalizer, `scripts/prime-health-parallel.py`) take
the path from the resolvers here, so the writer and its readers cannot drift
apart. The crm-logged log records conversations already logged as a CRM
interaction, so the CRM-log finalizer never writes one twice.
"""
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from scripts.record_readers._jsonl import append_jsonl, read_jsonl_capped
from scripts.utils.data_path import data_path
from scripts.utils.data_path import data_write_dir, data_write_file


# Conversations already recorded as a CRM interaction; append-only.
CRM_LOGGED_FILENAME = "_crm-logged.jsonl"
CRM_LOGGED_MAX_BYTES = 1_000_000
_CRM_LOGGED_LOCK = threading.Lock()

# Two routes to the same address, on purpose. The CREATING half goes through
# `data_write_dir`, which alone makes the store root 0700 with its README before
# anything lands under it (the logs hold subject lines, the state holds mail
# bodies; a bare mkdir takes the umask, 0755, MEASURED 2026-09-22). The ASKING
# half goes through `data_path`, which resolves the same path and creates nothing.
LATEST_FETCH_FILENAME = "_latest-fetch.json"


def crm_logged_path(data_root: Path, *, create: bool = False) -> Path:
    """`runtime:email-intel/logs` / `_crm-logged.jsonl` (created only on ``create=True``)."""
    if not create:
        return data_path("runtime:email-intel/logs", data_root=data_root) / CRM_LOGGED_FILENAME
    return data_write_dir("runtime:email-intel/logs", data_root=data_root) / CRM_LOGGED_FILENAME


def email_intel_state_dir(data_root: Path | None = None, *, create: bool = True) -> Path:
    """The /email-intel state directory, ``runtime:email-intel/state``.

    ``data_root=None`` resolves the writable overlay (and raises
    ``DataRootError`` on the bundled examples tree, where there is no store).
    ``create=False`` NAMES the directory without making it: a read must not
    materialise a directory (MEASURED 2026-09-14 on an empty scratch root).
    """
    if not create:
        return data_path("runtime:email-intel/state", data_root=data_root)
    return data_write_dir("runtime:email-intel/state", data_root=data_root)


def latest_fetch_path(data_root: Path | None = None, *, create: bool = False) -> Path:
    """The per-conversation fetch the crm_log finalizer reads.

    Nothing in the engine writes it any more (the `--unread` mode of
    `scripts/email-intelligence.py` was removed 2026-10-16); the outcome eval
    of `scripts/eval-outcomes.py` writes it into its sandbox. Defaults to NOT
    creating: every caller of this name reads.
    """
    return email_intel_state_dir(data_root, create=create) / LATEST_FETCH_FILENAME


def email_state_path(data_root: Path | None = None) -> Path:
    """The triage state (`processed_message_ids`, conversations, stats), registry name `email-intel-state`.

    Its directory is created here, as it always was. Its ``.lock`` sits beside
    it (``StateManager`` derives it with ``with_name``) and so does the
    ``.quarantine/`` a corrupt copy is moved into.
    """
    return data_write_file("email-intel-state", data_root=data_root)


def _read_jsonl(log_path: Path, max_bytes: int) -> list[dict]:
    """Read a jsonl log into a list of dict entries.

    Tolerant of corrupt lines. Over ``max_bytes`` it keeps the NEWEST entries
    rather than returning [] -- an empty result meant every deferred and
    CRM-logged conversation was instantly forgotten while writes kept
    succeeding, with nothing surfaced anywhere.
    """
    entries, _truncated = read_jsonl_capped(log_path, max_bytes)
    out: list[dict] = []
    out.extend(entries)
    return out


def _append_jsonl(resolve_log_path: Callable[[], Path], lock: threading.Lock,
                  entry: dict) -> tuple[bool, str | None]:
    """Append one JSON entry as a line to a jsonl log, under `lock`.

    Returns (True, None) on success or (False, error) on a write failure.
    The write itself is `_jsonl.append_jsonl`, a real O_APPEND line; `lock`
    only orders the writers inside this process. The path arrives as a
    zero-argument callable, not a Path, because the resolvers above CREATE
    the logs directory (`data_write_dir`) and that mkdir belongs inside the
    same try as the append: a path occupied by a plain file is a handled
    `(False, error)`, not a raise out of a function that promises the tuple.
    """
    with lock:
        try:
            append_jsonl(resolve_log_path(), entry)
        except OSError as e:
            return False, str(e)
    return True, None


def read_crm_logged(data_root: Path) -> set[str]:
    """Return conv_ids already logged as a CRM interaction (append-only set)."""
    out = set()
    for entry in _read_jsonl(crm_logged_path(data_root), CRM_LOGGED_MAX_BYTES):
        conv_id = entry.get("conv_id")
        if isinstance(conv_id, str) and conv_id:
            out.add(conv_id)
    return out


def mark_crm_logged(data_root: Path, conv_id: str, slug: str = "") -> tuple[bool, str | None]:
    """Record that `conv_id` was logged to CRM. Append-only, no undo."""
    entry = {
        "conv_id": conv_id,
        "slug": slug,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    return _append_jsonl(lambda: crm_logged_path(data_root, create=True), _CRM_LOGGED_LOCK, entry)
