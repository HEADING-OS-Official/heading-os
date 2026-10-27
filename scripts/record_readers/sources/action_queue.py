"""Action Queue source + mutate helpers (R1).

The Action Queue is where producers deposit drafted actions for a CEO go/no-go
from the terminal (`scripts/action-queue.py`). Backing store: ``queue.json``
(the authoritative card store) plus ``disposition-log.jsonl`` beside it (an
append-only audit trail), under ``runtime:action-queue/state``. Both paths come from
``queue_path`` / ``disposition_log_path`` below, which resolve through
``store_address``; nothing here spells the store's location as a literal.

- **These helpers are the only writers of ``queue.json``, and they run in SEVERAL
  processes** (``scripts/action-queue.py``, ``scripts/dead-letter.py`` and
  others). Every write takes ``_queue_lock``, which pairs a thread lock with an
  flock on ``queue.json.lock``, and writes atomically, so a read-modify-write is
  serialised between processes as well (lost update fixed 2026-08-27).
- **``append_cards`` is the sole dedup authority.** Callers never pre-dedup. A
  card is skipped when its contact already has a pending/approved card, or was
  dismissed within ``COOLDOWN_DAYS``.

Card schema (see plan Step 4): ``id, created_at, trace_id, source,
action_type(email_send|note), status(pending|approved|sent|dismissed|
send_failed), priority(P1|P2|P3), title, reasoning, citations[{source,
excerpt}], contact_file``; for ``email_send`` also ``to, subject, draft_body,
draft_status(needs_draft|ready_for_review)``. Mutations stamp ``approved_at /
dismissed_at / sent_at / error``.
"""
from __future__ import annotations

import contextlib
import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from scripts.utils.atomic import atomic_write_text
from scripts.record_readers._jsonl import append_jsonl
from scripts.utils import dead_letter, tool_risk, tracing
from scripts.utils.checkpoint_paths import file_lock
from scripts.utils.quarantine import quarantine_file, quarantine_ref
from scripts.utils.data_path import data_write_dir
from scripts.utils.runtime_dir import runtime_root
from scripts.utils.timeparse import parse_iso

QUEUE_FILENAME = "queue.json"
DISPOSITION_LOG_FILENAME = "disposition-log.jsonl"

#: The store's place under runtime-junk, spelled ONCE: `store_address` below is
#: the only reader of these four names, `queue_dir` creates through
#: `data_write_dir`, and `scripts/utils/ops_signals.queue_state` asks
#: `queue_path` rather than composing them a second time. It used to compose
#: them, for a reason its docstring gave as "a health probe must not materialise
#: the directory it asks about" -- which `queue_path`'s own `create=False`
#: default already guarantees, so the copy bought nothing and went blind to the
#: resolution below on 2026-09-20.
QUEUE_SERVICE = "action-queue"
QUEUE_KIND = "state"
QUEUE_SUBDIR = "action-queue"


def store_address(workspace_root: Path):
    """Where the store IS: ``runtime:action-queue/state`` / ``action-queue``, or a former address.

    The whole resolution lives in ``runtime_junk_moves.store_address``, which
    inverts the mover's own table; this names the four constants for it and is
    the one place in this module that does. Callers that need to TELL the
    operator which address answered (``scripts/action-queue.py``) ask this; the
    ones that only need the directory ask ``queue_dir``.

    MEASURED 2026-09-20 on the operator's overlay: 167 cards, 10 of them
    pending, sat at the pre-move address while this module composed the
    post-move one and ``action-queue.py list`` printed ``queue clear``. The
    mover that would have reconciled them shipped unrun in ``9de6d48a``
    (its own commit body: "The one-time move, NOT run"), and the family move
    then carried the old address itself under a family root.
    """
    # Imported here rather than at module level. Until 2026-09-28 that module
    # pulled the HEADING OS 2 mover and `subprocess` behind it; the mover is
    # retired and the import is kept lazy because nothing needs it earlier.
    from scripts.utils.runtime_junk_moves import store_address as resolve
    return resolve(runtime_root(workspace_root) / QUEUE_SERVICE / QUEUE_KIND / QUEUE_SUBDIR,
                   QUEUE_FILENAME, data_root=workspace_root)


def queue_dir(workspace_root: Path, *, create: bool = True) -> Path:
    """``runtime:action-queue/state`` / ``action-queue`` under ``workspace_root``, or where the store still is.

    ``workspace_root`` IS the data root here: the daemon passes ``data_root``,
    the CLIs pass ``get_data_root()``, and the tests inject a tmp tree. It is
    the one root every helper in this module takes, so the store follows it
    into a test sandbox and never lands in the engine checkout.

    ``create=False`` names it without making it. MEASURED 2026-09-14:
    ``action-queue.py list``, which only reads, materialised
    the store directory and its README on an empty
    root -- a write from a reader, which is what ``pulse.calendar_file`` refuses
    to do three modules away for the reason written beside it.

    When the store answers from a pre-move address, that address is returned for
    ``create=True`` as well, and nothing is created: the directory exists by
    definition, and a write must land beside the file the read came from. Both
    halves of every read-modify-write here derive their path from this one
    function, the ``queue.json.lock`` sidecar included, so the store cannot be
    split across two addresses under two different locks by a reader and a
    writer disagreeing. Moving it is HELM's, by hand, with the daemons stopped.
    """
    address = store_address(workspace_root)
    if address.legacy:
        return address.path
    if not create:
        return address.current
    target = data_write_dir("runtime:action-queue/state", data_root=workspace_root) / QUEUE_SUBDIR
    target.mkdir(exist_ok=True)
    return target


def queue_path(workspace_root: Path, *, create: bool = False) -> Path:
    """The authoritative card store. Its ``.lock`` and ``.quarantine/`` sit beside it.

    Defaults to NOT creating, because reading the queue is the common call and a
    read must leave no trace. The writers below ask for the directory.
    """
    return queue_dir(workspace_root, create=create) / QUEUE_FILENAME


def disposition_log_path(workspace_root: Path) -> Path:
    """The append-only audit trail beside the queue."""
    return queue_dir(workspace_root, create=True) / DISPOSITION_LOG_FILENAME

COOLDOWN_DAYS = 14            # dismissed contact not re-proposed within this window
PRUNE_TERMINAL_DAYS = 90     # drop terminal cards older than this (bound growth)
ROW_CAP = 100                # max active cards returned to the UI

# Which action_types may be deposited is DERIVED, at call time, from
# `config/tool-risk.json` via `tool_risk.classified_types()`. There is no tuple
# here any more.
#
# There was one until 2026-09-02: `("email_send", "note", "pipeline_update",
# "alert")`. It was a second copy of a taxonomy the ledger already owns, and a
# hand-maintained security-adjacent list is a list that falls behind in silence.
# MEASURED that day against the shipped ledger, it was four types short -
# `crm_write`, `knowledge_write`, `task_create` and `telegram_send` - and
# `append_cards` discarded every card carrying one of them while returning
# `{"ok": True, "added": 0, "skipped": 1}`.
#
# `telegram_send` is the one that shows the cost. It is `send_capable`, floors
# at `gated`, is shown in the UI, and `scripts/action-queue-execute.py` carries
# a 501 refusal branch built for it. It could not be deposited at all, so that
# branch had never been reachable through this door.
#
# Read at call time, never bound at import: `tool_risk` caches the ledger and
# re-reads on `load(force=True)`, and a module-level constant would freeze one
# process's first read for the life of that process.

# The CLAIM. A card sits here for the life of one synchronous send and nothing
# else may send it. Measured 2026-08-30 before it existed: two threads calling
# `approve_and_send` on one pending card both read the status, both passed the
# guard, and the sender ran TWICE on one card, each call returning `sent`.
#
# Deliberately NOT `approved`: `action-queue-execute.py` selects cards whose
# status IS `approved`, so claiming with it would hand the same card to the
# batch executor mid-send and rebuild the duplicate from the other end.
SENDING = "sending"

# How long a claim is believed before `approve` may take it over. Longer than
# `SEND_TIMEOUT_S` (120 s in action-queue-execute.py), which bounds the only slow
# step inside a claim, so a live sender is never overrun. Without a ceiling a
# terminal killed mid-send would strand its card in `sending`: `approve` refuses
# a claimed card and `retry` only runs on `send_failed`, so nothing could move
# it. Takeover happens on an explicit operator `approve`, never on its own.
STALE_CLAIM_SECONDS = 300

ACTIVE_STATUSES = ("pending", "approved", SENDING, "send_failed")

# A card is either ACTIVE or TERMINAL; there is no third state, and every writer
# of `status` has to land in one of these two tuples.
#
# `applied` (a notify card's auto-apply) is terminal, so it is counted and pruned
# like the others (2026-08-28).
TERMINAL_STATUSES = ("sent", "dismissed", "applied")
PRIORITY_ORDER = {"P1": 0, "P2": 1, "P3": 2}

# In-process lock: orders threads of one process. `_queue_lock` below adds the
# cross-process half (an flock), because several short-lived processes write the
# queue and a lost update can revert a `sent` card to `approved` (2026-06-27).
_LOCK = threading.Lock()

# Longer than file_lock's 2 s default. That default is tuned for a Stop hook with
# a 90-second budget; a queue mutation is interactive and a card mutation that
# waits ten seconds is far cheaper than one that proceeds unlocked and loses
# another process's write.
QUEUE_LOCK_WAIT_SECONDS = 10.0

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def _queue_lock(workspace_root: Path):
    """Hold BOTH locks for one read-modify-write of queue.json.

    Threads first, then the file lock, in that order everywhere - a mixed order
    between two call sites is how a deadlock is built.

    `file_lock` is bounded and never blocks forever: when the wait expires it
    proceeds UNLOCKED and says so on stderr. That is the right trade for a
    backup hook and the wrong silence for this store, so the degraded case also
    gets a log line naming the consequence.
    """
    with _LOCK:
        # The lock sidecar and the queue are both WRITES, so this one creates.
        queue = queue_path(workspace_root, create=True)
        lock_path = queue.with_name(queue.name + ".lock")
        with file_lock(lock_path, wait=QUEUE_LOCK_WAIT_SECONDS,
                       label="action-queue") as held:
            if not held:
                logger.warning(
                    "queue.json lock was busy for %.0fs; writing UNLOCKED. A "
                    "concurrent action-queue or dead-letter process "
                    "may lose this write or have its own erased.",
                    QUEUE_LOCK_WAIT_SECONDS,
                )
            yield held


# ============================================================
# Store IO (callers hold _queue_lock for any read-modify-write)
# ============================================================

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_queue() -> dict:
    return {"version": 1, "generated_at": None, "actions": []}


def _quarantine_corrupt_queue(path: Path, why: str) -> None:
    """Move an unreadable queue.json aside, loudly, before anything overwrites it.

    ABSENT and CORRUPT both used to return the empty default, and every mutating
    helper then atomically wrote that empty structure back. One torn write or a
    full disk therefore destroyed every pending and approved card -- drafted
    email bodies included -- with no error, no backup and no log line, while the
    endpoint still answered ``{"ok": true}``.

    Renaming costs nothing and keeps the wreck recoverable. The daemon carries
    on with an empty queue, which is the same behaviour as before; the
    difference is that the old cards still exist somewhere.

    WHERE it lands is the second half, and it was wrong until 2026-08-29. The
    wreck was `queue.json.corrupt-<stamp>` NEXT TO the live file: `queue.json`
    is gitignored in the data overlay precisely because it carries recipient
    addresses and whole drafted email bodies, and that name matched no rule in
    either repository. `push-all.py` runs `git add -A`, so one `GET
    /action-queue` over a torn file would have committed every pending draft
    into permanent history. `quarantine_file` puts it in a `.quarantine/`
    sibling, which both repositories ignore whole.

    Since 2026-09-14 the queue lives in the runtime store (`runtime:action-queue/state`
    since 2026-10-16), and
    the overlay TRACKS the store's `state/` and `logs/` kinds (only `cache/`
    and `tmp/` are ignored), so `queue.json` itself is no longer ignored by a
    rule of its own; whether the live `**/.quarantine/` rule stays is HELM's
    call. The wreck still goes beside the wreck.
    """
    try:
        target = quarantine_file(path)
    except OSError:
        logger.error(
            "queue.json is unreadable (%s) AND could not be moved aside; the "
            "next write will overwrite it and the pending cards are lost",
            why, exc_info=True,
        )
        return
    logger.error(
        "queue.json was unreadable (%s); moved to %s and starting from an empty "
        "queue. Recover any pending cards from that file by hand.",
        why, quarantine_ref(target),
    )


def _load_queue(workspace_root: Path) -> dict:
    """Read queue.json. Empty default when absent; quarantine when corrupt."""
    path = queue_path(workspace_root)
    if not path.exists():
        return _empty_queue()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        # `UnicodeDecodeError` is a `ValueError`, so neither `OSError` nor
        # `json.JSONDecodeError` caught it, and the decode fails inside
        # `read_text` before `json.loads` is ever reached. A torn write that
        # lands mid multi-byte character is the ORDINARY way this file goes
        # bad, and it was the one corruption that skipped the quarantine
        # entirely: the exception left `_load_queue`, `/action-queue` 500'd,
        # and the wreck stayed in the live path for the next writer to
        # overwrite. That is the exact loss `_quarantine_corrupt_queue` exists
        # to prevent, arriving through the door nobody had shut.
        _quarantine_corrupt_queue(path, f"{type(exc).__name__}: {exc}")
        return _empty_queue()
    if not isinstance(data, dict) or not isinstance(data.get("actions"), list):
        _quarantine_corrupt_queue(path, f"unexpected shape: {type(data).__name__}")
        return _empty_queue()
    return data


def _write_queue(workspace_root: Path, data: dict) -> None:
    data["generated_at"] = _now_iso()
    atomic_write_text(
        queue_path(workspace_root, create=True),
        json.dumps(data, indent=2, ensure_ascii=False),
        mode=0o600,  # may carry draft bodies + recipient addresses
    )


def _log_event(workspace_root: Path, event: dict) -> None:
    """Append one audit event to disposition-log.jsonl. Caller holds _queue_lock.

    Through the shared `append_jsonl` (O_APPEND), not a read-whole-file plus
    atomic rewrite. The rewrite was O(file size) PER EVENT, so the lifetime cost
    grew quadratically and peak memory was twice the log -- all of it under the
    global `_LOCK`, so a long-lived queue made every card mutation wait on a
    full re-serialisation of its own history. `approvals.py` already appends;
    this was the last rewriter.
    """
    event = {"ts": _now_iso(), "trace_id": tracing.get() or "-", **event}
    try:
        append_jsonl(disposition_log_path(workspace_root), event, mode=0o600)
    except OSError:
        # Audit trail is best-effort; never fail a mutation on a log write. But
        # a swallowed write is a hole in the trail, so it gets a line.
        logger.warning("could not append to the disposition log", exc_info=True)


def _dedup_key(card: dict) -> str | None:
    """Identity used for dedup: contact_file, else recipient, else title."""
    for k in ("contact_file", "to", "title"):
        v = card.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    return None


# ============================================================
# Public helpers (used by scripts/action-queue.py and the other CLIs)
# ============================================================

def append_cards(workspace_root: Path, cards: list[dict]) -> dict:
    """Append cards to the queue with dedup. THE sole dedup authority.

    Skips a card whose dedup key already has a pending/approved card, or was
    dismissed within COOLDOWN_DAYS. Normalises id/created_at/status/trace_id.
    Prunes terminal cards older than PRUNE_TERMINAL_DAYS.

    Two outcomes, and they are different shapes on purpose:

    - ``{ok: True, added, skipped, ids}`` - every card was ACCEPTABLE. ``skipped``
      now counts dedup and cooldown, and nothing else, which is the only thing
      the callers printing it have ever claimed it meant.
    - ``{ok: False, error, rejected, added: 0, skipped: 0, ids: []}`` - at least
      one card was not acceptable, and NOTHING was written. ``rejected`` carries
      one ``{index, action_type, reason}`` per bad card.

    A card is acceptable when it is an object carrying an ``action_type`` the
    ledger classifies (``tool_risk.classified_types()``). Validation runs BEFORE
    the lock and before any write, so a refusal is atomic: a partial write
    reported as a failure leaves a caller unable to say what is in the queue.

    Until 2026-09-02 an unrecognised type was counted into ``skipped`` and the
    call returned ``ok: True``. A caller could not separate "deduplicated
    against a live card", which is correct and expected, from "discarded because
    I do not know this type", which is a defect - and all three CLI callers
    print the count under a dedup label.

    Refusing the whole batch is the strict end of the two safe answers. The
    other is depositing an unknown type and letting ``tier_for`` floor it to
    ``gated``; that admits a typo'd ``action_type`` into the queue as a gated
    card with no executor behind it, so it is not taken here.
    """
    if not isinstance(cards, list):
        return {"ok": False, "error": "cards must be a list"}

    known = tool_risk.classified_types()
    rejected: list[dict] = []
    for i, raw in enumerate(cards):
        if not isinstance(raw, dict):
            rejected.append({"index": i, "action_type": None,
                             "reason": f"card is not an object ({type(raw).__name__})"})
            continue
        atype = raw.get("action_type")
        if atype not in known:
            rejected.append({"index": i, "action_type": atype,
                             "reason": "action_type is not classified in "
                                       "config/tool-risk.json"})
    if rejected:
        summary = ", ".join(f"#{r['index']} {r['action_type']!r}" for r in rejected)
        # ERROR, not a silent return. Three of the callers here are short-lived
        # CLIs that configure no logging, where `logging.lastResort` puts this on
        # stderr - the only channel that reaches an operator who is reading a
        # line printed from `res["added"]` and `res["skipped"]` alone.
        logger.error(
            "refusing to deposit %d card(s) of %d: %s. Nothing was written. An "
            "action_type must be classified in config/tool-risk.json before a "
            "card carrying it can be queued.",
            len(rejected), len(cards), summary,
        )
        return {"ok": False,
                "error": f"{len(rejected)} card(s) carry an action_type that is "
                         f"not classified in config/tool-risk.json: {summary}",
                "rejected": rejected, "added": 0, "skipped": 0, "ids": []}

    now = datetime.now(timezone.utc)
    added_ids: list[str] = []
    skipped = 0
    with _queue_lock(workspace_root):
        data = _load_queue(workspace_root)
        actions = data["actions"]

        # Index existing cards by dedup key for O(1)-ish lookup.
        by_key: dict[str, list[dict]] = {}
        for c in actions:
            k = _dedup_key(c)
            if k:
                by_key.setdefault(k, []).append(c)

        # Every card here is already known to be an object with a classified
        # action_type - the validation pass above returned rather than fall
        # through. `skipped` from this point on means dedup or cooldown, and
        # re-adding a shape check here would put the two reasons back into one
        # number.
        for raw in cards:
            key = _dedup_key(raw)
            if key and key in by_key:
                existing = by_key[key]
                # Already live -> skip. Read from ACTIVE_STATUSES, never from a
                # second copy of it: this was the literal ("pending",
                # "approved"), which left out `send_failed`. A send_failed card
                # IS live - every lister shows it, and `SENDABLE_STATUSES` in
                # scripts/action-queue.py includes it, so the CEO can approve it
                # - and a re-deposit of the same dedup key therefore created a
                # SECOND live card for the same contact. Approving both mails
                # the person twice, which is the one outcome this dedup exists
                # to stop.
                if any(c.get("status") in ACTIVE_STATUSES for c in existing):
                    skipped += 1
                    continue
                # Dismissed within cooldown -> skip (re-propose suppression).
                in_cooldown = False
                for c in existing:
                    if c.get("status") == "dismissed":
                        dt = parse_iso(c.get("dismissed_at"))
                        if dt and (now - dt).days < COOLDOWN_DAYS:
                            in_cooldown = True
                            break
                if in_cooldown:
                    skipped += 1
                    continue

            card = dict(raw)
            card["id"] = card.get("id") or uuid.uuid4().hex
            card["created_at"] = card.get("created_at") or _now_iso()
            card["status"] = "pending"
            card["tier"] = tool_risk.tier_for(card["action_type"])
            card.setdefault("trace_id", tracing.get() or "-")
            card.setdefault("priority", "P3")
            card.setdefault("citations", [])
            actions.append(card)
            added_ids.append(card["id"])
            if key:
                by_key.setdefault(key, []).append(card)

        # Prune old terminal cards to bound growth.
        cutoff = now
        kept = []
        for c in actions:
            if c.get("status") in TERMINAL_STATUSES:
                stamp = parse_iso(c.get("sent_at") or c.get("dismissed_at")
                                  or c.get("applied_at") or c.get("created_at"))
                if stamp and (cutoff - stamp).days > PRUNE_TERMINAL_DAYS:
                    continue
            kept.append(c)
        data["actions"] = kept

        _write_queue(workspace_root, data)
        if added_ids:
            _log_event(workspace_root, {"event": "deposit", "added": added_ids, "skipped": skipped})
    return {"ok": True, "added": len(added_ids), "skipped": skipped, "ids": added_ids}


def _find(actions: list[dict], action_id: str) -> dict | None:
    for c in actions:
        if c.get("id") == action_id:
            return c
    return None


def apply_status(workspace_root: Path, action_id: str, status: str,
                 event: str | None = None, **fields) -> dict:
    """Set a card's status (+ optional extra fields) atomically and log it.

    Used by approve/dismiss and by the executor-result application
    (sent / send_failed). Returns {ok, card} or {ok: False, error}.

    `status` is CHECKED against the two tuples, and the check runs before the
    lock and before any write, so a refusal writes nothing. The comment above
    `TERMINAL_STATUSES` states the invariant this enforces: a card is either
    ACTIVE or TERMINAL, there is no third state, and every writer of `status`
    has to land in one of the two. Nothing held the writers to it until
    2026-09-02, and this is the writer four callers reach for.

    What a third state cost, on a card stamped `"approve"` for `"approved"`:
    `list_action_queue` filters on ACTIVE_STATUSES, so the card vanished from
    the only surface that could approve or dismiss it; the prune in
    `append_cards` filters on TERMINAL_STATUSES, so it was never dropped at any
    age, under a comment promising bound growth; and the dedup check in
    `append_cards` also reads ACTIVE_STATUSES, so re-depositing the same key
    minted a SECOND live card. That last one is the duplicate outbound message
    the dedup exists to stop. The call returned `{"ok": True}` throughout.
    """
    if not action_id:
        return {"ok": False, "error": "action_id required"}
    if status not in ACTIVE_STATUSES and status not in TERMINAL_STATUSES:
        return {"ok": False,
                "error": f"unknown status {status!r}; must be one of "
                         f"{ACTIVE_STATUSES + TERMINAL_STATUSES}"}
    with _queue_lock(workspace_root):
        data = _load_queue(workspace_root)
        card = _find(data["actions"], action_id)
        if card is None:
            return {"ok": False, "error": "not found"}
        card["status"] = status
        # `applied` joined this map on 2026-08-28. Without a stamp of its own the
        # prune fell back to `created_at`, so an auto-apply that happened today
        # on a card deposited four months ago was eligible for pruning the
        # instant it became terminal. Every terminal status now dates itself.
        stamp_field = {
            "approved": "approved_at",
            "dismissed": "dismissed_at",
            "sent": "sent_at",
            "applied": "applied_at",
        }.get(status)
        if stamp_field:
            card[stamp_field] = _now_iso()
        for k, v in fields.items():
            card[k] = v
        _write_queue(workspace_root, data)
        _log_event(workspace_root, {"event": event or status, "action_id": action_id})
        # A permanently-failed send becomes a durable, trace-keyed dead-letter
        # artifact instead of vanishing (R14). dead_letter.record never raises.
        if status == "send_failed" and fields.get("classification") == "permanent":
            dead_letter.record(
                trace_id=card.get("trace_id") or "-",
                kind=card.get("action_type") or "unknown",
                payload=card,
                classification="permanent",
                error=str(card.get("error") or fields.get("error") or ""),
                workspace_root=workspace_root,
            )
    return {"ok": True, "card": card}


def _claim_age_seconds(card: dict, now: datetime | None = None) -> float | None:
    """Seconds since this card was claimed, or None when that cannot be read.

    A stamp in the FUTURE returns a negative number rather than a clamped zero,
    and every caller treats "below the stale window" as still held, so a skewed
    stamp refuses the takeover instead of granting it. That is the safe
    direction here: the cost of refusing is a card the operator must dismiss,
    and the cost of granting is a second copy of an email already sent.
    """
    stamp = parse_iso(card.get("sending_since"))
    if stamp is None:
        return None
    now = datetime.now(timezone.utc) if now is None else now
    return (now - stamp).total_seconds()


def claim_card_for_send(workspace_root: Path, action_id: str,
                        sendable, stale_after: float = STALE_CLAIM_SECONDS,
                        now: datetime | None = None) -> dict:
    """Atomically move one card from a sendable status to ``SENDING``.

    THE compare-and-set that closes the concurrent-approve race. The status
    check and the status write happen inside ONE ``_queue_lock``, so a second
    approve of the same card finds it already ``SENDING`` and is refused. Read
    and write used to sit in different locks with a send between them, and two
    approves therefore both passed the check and both sent (measured
    2026-08-30: sender invoked twice, both calls returning ``sent``).

    A claim older than ``stale_after`` is taken over: it can only mean the
    process holding it died, since nothing inside a claim outlives the sender's
    own 120-second timeout. A claim whose ``sending_since`` cannot be read is
    NOT taken over - absent and stale are different facts, and guessing costs a
    duplicate outbound message - so the refusal names ``dismiss`` as the way out.

    Returns {ok: True, card, prev_status} or {ok: False, error, status}.
    """
    if not action_id:
        return {"ok": False, "error": "action_id required", "status": None}
    with _queue_lock(workspace_root):
        data = _load_queue(workspace_root)
        card = _find(data["actions"], action_id)
        if card is None:
            return {"ok": False, "error": "not found", "status": None}
        status = card.get("status")
        if status == SENDING:
            held = _claim_age_seconds(card, now=now)
            if held is None:
                return {"ok": False, "status": status,
                        "error": ("card is already 'sending' and carries no "
                                  "readable claim time, so it cannot be shown "
                                  "abandoned; dismiss it if no send is running")}
            if held < stale_after:
                return {"ok": False, "status": status,
                        "error": (f"another approve claimed this card {int(held)}s "
                                  f"ago and may still be sending; the claim frees "
                                  f"after {int(stale_after)}s")}
        elif status not in sendable:
            return {"ok": False, "status": status,
                    "error": (f"card is {status!r}; approve only sends a card in "
                              f"{sorted(sendable)}")}
        card["status"] = SENDING
        card["sending_since"] = _now_iso()
        _write_queue(workspace_root, data)
        _log_event(workspace_root, {"event": "claim", "action_id": action_id,
                                    "from": status})
        return {"ok": True, "card": dict(card), "prev_status": status}


def release_claim(workspace_root: Path, action_id: str, prev_status: str) -> dict:
    """Undo a claim, for the path where the send never ran.

    Only the sender raising something nobody planned for reaches here. Putting
    the card back where the claim found it reproduces the behaviour that path
    had before claiming existed, rather than inventing a `send_failed` about a
    send that was never attempted.

    ``prev_status`` may itself be ``SENDING`` (this claim was a takeover). The
    card then keeps THIS claim's fresh timestamp and goes stale again on the
    normal schedule, so it is never stranded.
    """
    if not action_id or not prev_status:
        return {"ok": False, "error": "action_id and prev_status required"}
    with _queue_lock(workspace_root):
        data = _load_queue(workspace_root)
        card = _find(data["actions"], action_id)
        if card is None:
            return {"ok": False, "error": "not found"}
        card["status"] = prev_status
        if prev_status != SENDING:
            card.pop("sending_since", None)
        _write_queue(workspace_root, data)
        _log_event(workspace_root, {"event": "claim_released",
                                    "action_id": action_id, "to": prev_status})
    return {"ok": True, "card": card}


def annotate_card(workspace_root: Path, action_id: str, **fields) -> dict:
    """Stamp arbitrary advisory fields onto a card WITHOUT changing its status.

    Distinct from ``apply_status`` (state transitions + the DLQ side-effect) and
    from ``edit_card`` (rewrites an email draft). ``annotate_card`` attaches
    advisory metadata - the R5b pre-approval ``critique`` is the first consumer -
    and is *structurally incapable* of changing ``status``: a ``status`` key in
    ``fields`` is dropped before the write. This preserves the lethal-trifecta
    control - an advisory layer can annotate, never approve/dismiss/send. Atomic
    under ``_queue_lock`` + logged. Returns {ok, card} or {ok: False, error}.
    """
    if not action_id:
        return {"ok": False, "error": "action_id required"}
    fields.pop("status", None)  # an annotation can never be a state transition
    if not fields:
        return {"ok": False, "error": "no fields to annotate"}
    with _queue_lock(workspace_root):
        data = _load_queue(workspace_root)
        card = _find(data["actions"], action_id)
        if card is None:
            return {"ok": False, "error": "not found"}
        for k, v in fields.items():
            card[k] = v
        _write_queue(workspace_root, data)
        _log_event(workspace_root, {"event": "annotate", "action_id": action_id})
    return {"ok": True, "card": card}


def edit_card(workspace_root: Path, action_id: str, *, subject: str | None = None,
              draft_body: str | None = None, draft_status: str | None = None,
              to: str | None = None) -> dict:
    """Rewrite an email card's recipient / subject / draft_body (and optionally
    flip draft_status). Atomic + logged. Returns {ok, card} or {ok: False, error}.

    ``to`` is here because a depositor may not know the recipient yet. Without
    it the only correction path for a placeholder address was hand-editing
    queue.json, which every skill in this workspace is told never to do.
    Plausibility of the address is the CALLER's check (see
    ``scripts/utils/recipient.py``); this helper only writes what it is given.
    """
    if not action_id:
        return {"ok": False, "error": "action_id required"}
    with _queue_lock(workspace_root):
        data = _load_queue(workspace_root)
        card = _find(data["actions"], action_id)
        if card is None:
            return {"ok": False, "error": "not found"}
        if to is not None:
            card["to"] = to
        if subject is not None:
            card["subject"] = subject
        if draft_body is not None:
            card["draft_body"] = draft_body
        if draft_status is not None:
            card["draft_status"] = draft_status
        _write_queue(workspace_root, data)
        _log_event(workspace_root, {"event": "edit", "action_id": action_id})
    return {"ok": True, "card": card}


def approve_card(workspace_root: Path, action_id: str) -> dict:
    """Mark a card approved. Does NOT send - the executor does (off the request
    path). ``note`` cards approve to a no-op disposition."""
    return apply_status(workspace_root, action_id, "approved", event="approved")


def dismiss_card(workspace_root: Path, action_id: str, reason: str = "") -> dict:
    """Tombstone a card (status dismissed). Starts the re-propose cooldown."""
    fields = {}
    safe = (reason or "").replace("\n", " ").replace("\r", " ").strip()[:200]
    if safe:
        fields["dismiss_reason"] = safe
    return apply_status(workspace_root, action_id, "dismissed", event="dismissed", **fields)


# ============================================================
# Read path
# ============================================================

def _card_tier(card: dict) -> str:
    """Resolve a card's risk tier, recomputing from action_type when the
    stored ``tier`` is absent (legacy cards) so banding is never wrong."""
    tier = card.get("tier")
    if isinstance(tier, str) and tier:
        return tier
    return tool_risk.tier_for(card.get("action_type", ""))


def list_action_queue(workspace_root: Path) -> dict:
    """Return the active queue envelope, banded by risk tier.

    Active = pending / approved / send_failed, pending first, then by priority,
    then created_at.

    ``items`` / ``total`` stay the full active set, because callers find a card
    by id there. The same set is ALSO split into two lanes:

      - ``actionable``: gated tier (``email_send`` / ``telegram_send``) - the
        approve/send lane. ``actionable_total`` counts these.
      - ``fyi``:        autonomous / notify tier (``note`` / ``alert`` /
        ``pipeline_update``) - read-only context. ``fyi_total`` counts these.

    Every TERMINAL status (sent, dismissed, applied) is summarised as a count.
    """
    data = _load_queue(workspace_root)
    actions = data.get("actions", [])
    active = [c for c in actions if c.get("status") in ACTIVE_STATUSES]
    sent_count = sum(1 for c in actions if c.get("status") == "sent")
    dismissed_count = sum(1 for c in actions if c.get("status") == "dismissed")
    applied_count = sum(1 for c in actions if c.get("status") == "applied")

    def _sort_key(c: dict):
        status_rank = 0 if c.get("status") == "pending" else 1
        prio = PRIORITY_ORDER.get(c.get("priority", "P3"), 9)
        return (status_rank, prio, c.get("created_at", ""))

    active.sort(key=_sort_key)
    actionable = [c for c in active if _card_tier(c) == "gated"]
    fyi = [c for c in active if _card_tier(c) != "gated"]
    return {
        # NOT capped: `scripts/action-queue.py` finds a card by id here, and a
        # cap hid the oldest cards from it. ROW_CAP bounds the two lanes only.
        "items": active,
        "total": len(active),
        "actionable": actionable[:ROW_CAP],
        "actionable_total": len(actionable),
        "fyi": fyi[:ROW_CAP],
        "fyi_total": len(fyi),
        "sent_count": sent_count,
        "dismissed_count": dismissed_count,
        "applied_count": applied_count,
        "data_time": data.get("generated_at"),
    }
