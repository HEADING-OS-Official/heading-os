#!/usr/bin/env python3
"""ops_signals.py - pure, dir-parameterized state computation for ops-radar.

One function per signal that the ops-radar detector aggregates. Each returns a
flat dict of the shape:

    {key, value, threshold, due, severity, tier, summary}

`summary` is a COUNTS-ONLY one-liner (no content, no PII) safe to put on the
Telegram wire. `severity` is one of SEVERITY_ORDER (ok < warn < high < critical)
and drives the ack "band" comparison and the crunch critical-floor. `tier` is
"A" (machine-domain, auto-healable) or "B" (sovereign manual action, nudge-only).

Design split (per plan Decision 8 + testability): the expensive / non-
deterministic measurement (git plumbing, an ollama probe, a subprocess) is kept
separate from the PURE classifier that turns measured primitives into the signal
dict. The classifiers (`classify_backup`, `classify_ollama`, `classify_ollama_accel`,
`classify_index`, `classify_weekly_review`, `classify_odin`)
are unit-tested in isolation; the measurement wrappers (`backup_state`,
`ollama_state`, ...) call them after gathering primitives.

READ ONLY. No function here mutates workspace state. Consumed by
scripts/ops-radar.py.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from scripts.record_readers.sources.action_queue import SENDING, queue_path
from scripts.utils.odin_cadence import read_cadence_json
from scripts.utils.data_homes import homes
from scripts.utils.data_path import DataPathError, data_path, data_read
from scripts.utils.memory_layers import resolve_layers
from scripts.utils.runtime_dir import runtime_root

# ============================================================
# Severity + thresholds
# ============================================================

# Ordered weakest -> strongest. The crunch critical-floor is severity == "critical".
SEVERITY_ORDER = ["ok", "warn", "high", "critical"]


def severity_rank(sev: str) -> int:
    """Numeric rank of a severity label (unknown -> 0)."""
    return SEVERITY_ORDER.index(sev) if sev in SEVERITY_ORDER else 0


# Tier-B (sovereign manual) thresholds.
BACKUP_UNCOMMITTED_HOURS = 24      # uncommitted work sitting this long is due
BACKUP_HIGH_HOURS = 48             # ... escalates to high
BACKUP_CRITICAL_HOURS = 72         # ... pierces crunch (imminent data-loss floor)
WEEKLY_REVIEW_DAYS = 7             # days since last review file -> due
WEEKLY_REVIEW_HIGH_DAYS = 14

# Daemon health thresholds (Tier B: surfaced, never healed; restarting a unit
# is a daemon lifecycle action and stays the operator's). Proposals, not
# measurements, set 2026-10-12.
RESTART_LOOP_MAX = 5               # automatic restarts of one user unit in the window -> due
RESTART_LOOP_WINDOW_HOURS = 24     # the radar runs daily, so the window covers one run
EXCHANGE_SYNC_STALE_HOURS = 6      # no successful Exchange sync this long -> due (the job is 2-hourly)
EXCHANGE_SYNC_HIGH_HOURS = 24      # ... escalates to high

# Tier-A (machine-domain, auto-heal) thresholds.
INDEX_STALE_DAYS = 2               # index older than this (build age) -> rebuild
AUTOHEAL_ESCALATE = 2              # consecutive auto-heal failures before surfacing in the nudge

# Fallback endpoint for `ollama_state` when this machine pins no host. It is NOT
# "the local daemon by policy" any more: on 2026-08-23 the ollama inside WSL was
# uninstalled and every model moved to the Windows side, so a monitor hardwired
# to localhost would have gone red forever and driven Tier-A to keep restarting
# a unit that no longer exists. `ollama_state` now probes whatever
# `config/inference-hosts.yaml` says serves this machine, and falls back here only
# on a machine that pins nothing.
OLLAMA_HOST = "http://localhost:11434"

# The model name is NOT a constant here (a hardcoded one reported a model no
# longer in use, removed 2026-08-22): read it per call from
# `embeddings.index_embed_model()`, which reads the config and probes nothing.


# ============================================================
# Tier-B: backup (git, both repos)
# ============================================================

def classify_backup(uncommitted: int, oldest_age_hours: float | None, ahead: int,
                    unreadable: int = 0) -> dict:
    """Pure: turn measured git primitives into the backup signal dict.

    due when uncommitted work has sat >= BACKUP_UNCOMMITTED_HOURS, OR any commit
    is unpushed (ahead > 0). Severity escalates with the age of the oldest
    uncommitted change; >= BACKUP_CRITICAL_HOURS is the crunch-piercing floor.

    Two arguments carry "I could not measure this", and both escalate rather
    than reassure:

    `oldest_age_hours=None` means dirty paths exist and NONE of them could be
    stat'd. That is the ordinary case for deletions: `git status --porcelain`
    lists " D f1.txt" and the file is gone, so the old code skipped every entry,
    left `oldest_mtime` at None and reported 0.0 hours. Three files deleted a
    week ago read as "3 uncommitted (0h old)" and not due, because due needs an
    age >= BACKUP_UNCOMMITTED_HOURS. Measured 2026-08-26.

    `unreadable` counts repos whose `git status` itself failed. That returned
    (0, 0.0) and summed into a clean total, so a repo git could not read at all
    reported as a repo with nothing to back up.
    """
    age_known = oldest_age_hours is not None
    age = oldest_age_hours if age_known else 0.0
    stale = uncommitted > 0 and (not age_known or age >= BACKUP_UNCOMMITTED_HOURS)
    due = stale or ahead > 0 or unreadable > 0
    if uncommitted > 0 and age_known and age >= BACKUP_CRITICAL_HOURS:
        severity = "critical"
    elif (unreadable > 0
          or (uncommitted > 0 and not age_known)
          or (uncommitted > 0 and age >= BACKUP_HIGH_HOURS)):
        severity = "high"
    elif due:
        severity = "warn"
    else:
        severity = "ok"
    age_text = f"{age:.0f}h old" if age_known else "age unknown"
    summary = (f"backup: {uncommitted} uncommitted ({age_text}), {ahead} unpushed")
    if unreadable:
        summary += f", {unreadable} repo(s) git could not read"
    return {
        "key": "backup",
        "value": {
            "uncommitted": uncommitted,
            "oldest_age_hours": round(age, 1) if age_known else None,
            "ahead": ahead,
            "unreadable": unreadable,
        },
        "threshold": BACKUP_UNCOMMITTED_HOURS,
        "due": due,
        "severity": severity,
        "tier": "B",
        "summary": summary,
    }


def _run_git_bytes(repo: Path, args: list[str]) -> tuple[int, bytes]:
    """Run git in `repo`, return (returncode, raw stdout). Never raises.

    Raw, because a caller reading PATHS must not go through subprocess text
    mode. Text mode turns on universal newlines and rewrites every CR byte to
    LF, and `subprocess` exposes no `newline=` knob to switch it off. MEASURED
    2026-08-30: two tracked files `docs/x\\r\\ny.md` and `docs/x\\ny.md` come back
    from `status --porcelain -z` as two distinct records in bytes mode and as one
    under `text=True`. `-z` does not help; it suppresses git's C-quoting, and
    the translation happens afterwards, in Python.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, b""
    return proc.returncode, proc.stdout


def _run_git(repo: Path, args: list[str]) -> tuple[int, str]:
    """Run git in `repo`, return (returncode, decoded stdout). Never raises.

    For callers reading counts and refs. A caller reading PATHS wants
    `_run_git_bytes` and its own decode - see the docstring there.
    """
    rc, raw = _run_git_bytes(repo, args)
    return rc, raw.decode("utf-8", "surrogateescape")


def _repo_uncommitted(repo: Path) -> tuple[int | None, float | None]:
    """Return (uncommitted_count, oldest_age_hours) for one git repo.

    Two None values, and each means something different from zero:

    - count None: `git status` itself failed, so nothing was measured. This used
      to be folded in with "clean" and returned (0, 0.0), which is how a repo
      with an unreachable gitdir reported "0 uncommitted". Reproduced 2026-08-26
      with a `.git` file pointing at a directory that does not exist: git exits
      128 and prints nothing on stdout.
    - age None: dirty paths exist and none of them could be stat'd. Deletions
      are the common case, and reporting 0.0 hours for them says "just now",
      which is the reading that keeps the signal quiet.

    oldest_age_hours = now minus the OLDEST mtime among the dirty paths (how long
    work has been sitting).
    """
    # `-z`, not plain porcelain. Git C-QUOTES any path holding a space, a
    # non-ASCII byte or a backslash: `?? "caf\303\251.md"`, quotes and octal
    # escapes included. Those bytes are not the filename, so `stat()` raised
    # FileNotFoundError and the path was dropped from the oldest-mtime scan
    # entirely - understating backup debt in exactly the way this function's
    # docstring says must not happen. `-z` suppresses quoting and separates
    # records with NUL.
    #
    # Read as BYTES and decoded here, because `-z` closes only half of it. Any
    # subprocess text mode turns on universal newlines and rewrites each CR byte
    # to LF (measured 2026-08-30; see `_run_git_bytes`), so a dirty path holding
    # a CR arrived spelled as a file that does not exist, `stat()` raised
    # FileNotFoundError, and the path was dropped from the oldest-mtime scan --
    # the same understated backup debt, by the other door.
    rc, raw = _run_git_bytes(repo, ["status", "--porcelain", "-z"])
    if rc != 0:
        return None, None
    out = raw.decode("utf-8", "surrogateescape")
    # Under -z a rename is TWO NUL-separated fields for ONE entry: "R  <new>"
    # then "<old>". Consume the second so it is neither counted as a change nor
    # stat'd as a path that no longer exists.
    fields = [f for f in out.split("\0") if f]
    if not fields:
        return 0, 0.0
    entries: list[str] = []
    i = 0
    while i < len(fields):
        record = fields[i]
        entries.append(record)
        if record[:1] in ("R", "C"):
            i += 1  # skip the source path that follows
        i += 1
    now = time.time()
    oldest_mtime = None
    for record in entries:
        # porcelain record: "XY <path>"; the path starts at column 3, verbatim.
        #
        # NOT `.strip()`. That reintroduced exactly the corruption `-z` was
        # chosen to avoid: a filename with a leading or trailing space is a legal
        # filename, and stripping it made `stat()` raise FileNotFoundError, so
        # `except OSError: continue` dropped the path from the oldest-mtime scan
        # in silence. Measured 2026-08-30 in a scratch repo holding
        # ` lead.md` (200h old), `old .md` (100h) and `fresh.md`: the oldest
        # sitting change reported as 100h instead of 200h, understating backup
        # debt in the direction that keeps the signal quiet.
        path = record[3:]
        fp = repo / path
        try:
            mt = fp.stat().st_mtime
        except OSError:
            continue
        if oldest_mtime is None or mt < oldest_mtime:
            oldest_mtime = mt
    age_hours = (now - oldest_mtime) / 3600.0 if oldest_mtime is not None else None
    return len(entries), age_hours


def _repo_ahead(repo: Path) -> int:
    """Commits on HEAD not on the upstream (or origin/main fallback)."""
    rc, out = _run_git(repo, ["rev-list", "--count", "@{u}..HEAD"])
    if rc != 0:
        rc, out = _run_git(repo, ["rev-list", "--count", "origin/main..HEAD"])
    if rc != 0:
        return 0
    try:
        return int(out.strip() or "0")
    except ValueError:
        return 0


def backup_state(engine_root: Path, data_root: Path) -> dict:
    """Measure git backup debt across BOTH repos, then classify.

    Aggregates the engine clone and the data overlay: total uncommitted, the
    oldest sitting change across both, and total unpushed commits.
    """
    repos = [engine_root]
    if data_root.resolve() != engine_root.resolve():
        repos.append(data_root)
    total_uncommitted = 0
    oldest_age: float | None = 0.0
    total_ahead = 0
    unreadable = 0
    for repo in repos:
        if not (repo / ".git").exists():
            continue
        n, age = _repo_uncommitted(repo)
        if n is None:
            # git could not read this repo at all. Counted and reported; the old
            # code summed its (0, 0.0) into the totals and said nothing.
            unreadable += 1
            total_ahead += _repo_ahead(repo)
            continue
        total_uncommitted += n
        if age is None:
            oldest_age = None  # unknown wins: it can only be older, never newer
        elif oldest_age is not None:
            oldest_age = max(oldest_age, age)
        total_ahead += _repo_ahead(repo)
    return classify_backup(total_uncommitted, oldest_age, total_ahead, unreadable)


# ============================================================
# Tier-B: weekly review (fs)
# ============================================================

def classify_weekly_review(days_since: int | None) -> dict:
    """Pure: days since the newest weekly-review file -> signal dict.

    None means no review has ever been written (treated as due, high)."""
    if days_since is None:
        due, severity = True, "high"
        value = "never"
    else:
        due = days_since >= WEEKLY_REVIEW_DAYS
        if days_since >= WEEKLY_REVIEW_HIGH_DAYS:
            severity = "high"
        elif due:
            severity = "warn"
        else:
            severity = "ok"
        value = days_since
    return {
        "key": "weekly_review",
        "value": value,
        "threshold": WEEKLY_REVIEW_DAYS,
        "due": due,
        "severity": severity,
        "tier": "B",
        "summary": (
            "weekly-review: never run" if days_since is None
            else f"weekly-review: {days_since}d since last"
        ),
    }


def weekly_review_state(data_root: Path, now: float | None = None) -> dict:
    """Days since the newest weekly review on disk, wherever it is filed today.

    The reviews are asked of the registry by name, `weekly-reviews`, against
    the data root this signal is handed, so the caller's root and the address
    derive from one argument (HOS-77). What must match the /weekly-review skill
    is the filename shape, `YYYY-MM-DD-weekly-review.md`, and the directory the
    name places. A prior mismatch (this read an `operations/weekly-review/`
    component the skill never wrote) made the signal report "never run" even
    when reviews existed.

    Until 2026-09-25 (HOS-5 section 6) this built the PRE-MOVE spelling,
    `<data root>/outputs/operations/reviews`, and handed it to the move's
    compatibility layer, which read the whole of `("trace", "report")` and
    `("trace", "scratch")` and kept the records whose legacy path lay under it. The
    name answers the same directories directly, including the legacy one on a
    tree with no manifest, and a review is always `("trace", "report")`: the
    classifier files an operations record under `report` when its path carries
    a hint word, and every review's filename carries `review`.
    """
    now = time.time() if now is None else now
    newest = None
    for p in _review_files(data_read("weekly-reviews", data_root=data_root)):
        try:
            mt = p.stat().st_mtime
        except OSError:
            continue
        if newest is None or mt > newest:
            newest = mt
    days_since = None if newest is None else int((now - newest) // 86400)
    return classify_weekly_review(days_since)


def _review_files(review_dirs: tuple[Path, ...]) -> list[Path]:
    """Every file under the directories the name answers, walked recursively.

    Recursive because the reviews are filed in dated subdirectories and the
    walk this replaced was `rglob`. An address that is not on disk contributes
    nothing, which is the legitimate empty answer `data_read` documents.
    """
    found: dict[Path, None] = {}
    for review_dir in review_dirs:
        if review_dir.is_dir():
            for p in sorted(review_dir.rglob("*")):
                if p.is_file():
                    found[p] = None
    return list(found)


# ============================================================
# Tier-B: Odin cadence (wrapper over odin-cadence.py --json)
# ============================================================

def classify_odin(cadence: dict) -> dict:
    """Pure: odin-cadence.py --json result -> signal dict.

    `0 clusters` used to be the whole sentence, and it is two different states
    wearing one number. `analyze_reflect_clusters` skips a component either
    because fewer than two raw episodes share enough tags to form one, or
    because every member predates `.last-reflect` and the CEO has already looked
    at it. Both `continue` statements land on the same 0. The first says the
    brain has nothing to reflect on; the second says the reflect side is CURRENT,
    which is the good outcome and read, daily, as the empty one. MEASURED
    2026-09-06: two clusters of three episodes each existed, were reviewed on
    2026-08-11, were deliberately not graduated, and the radar row said
    `0 clusters` for twenty-six days.

    `reviewed_clusters` is read with `.get` returning None rather than 0, and
    the parenthetical is omitted when it is None. A cadence dict produced before
    2026-09-06 does not carry the field, and rendering `(0 reviewed)` for it
    would assert something this function never established.
    """
    nudge = bool(cadence.get("nudge"))
    total = cadence.get("unharvested_total", 0)
    clusters = cadence.get("reflect_clusters", 0)
    reviewed = cadence.get("reviewed_clusters")
    stale = cadence.get("stale_clusters", 0)
    if stale >= 1:
        severity = "high"
    elif nudge:
        severity = "warn"
    else:
        severity = "ok"
    if clusters:
        qualifier = f" ({stale} stale)" if stale else ""
    elif reviewed is None:
        qualifier = ""
    elif reviewed:
        qualifier = f" ({reviewed} reviewed, none new)"
    else:
        qualifier = " (none formed)"
    return {
        "key": "odin_cadence",
        "value": {"unharvested": total, "clusters": clusters,
                  "reviewed": reviewed, "stale": stale},
        "threshold": cadence.get("min_entries", 0),
        "due": nudge,
        "severity": severity,
        "tier": "B",
        "summary": f"odin: {total} un-harvested, {clusters} clusters{qualifier}",
    }


def classify_queue(ready: int, failed: int) -> dict:
    """Pure: Action Queue drafts awaiting the CEO -> signal dict (Tier B).

    due when >= 1 draft is ready_for_review OR >= 1 card is send_failed. A failed
    send escalates to high (it needs attention, not just a nudge)."""
    due = ready >= 1 or failed >= 1
    severity = "high" if failed >= 1 else ("warn" if due else "ok")
    summary = f"queue: {ready} draft(s) ready" + (f" ({failed} failed)" if failed else "")
    return {
        "key": "queue",
        "value": {"ready": ready, "failed": failed},
        "threshold": 1,
        "due": due,
        "severity": severity,
        "tier": "B",
        "summary": summary,
    }


def queue_state(data_root: Path) -> dict:
    """Count Action Queue cards awaiting the CEO (ready_for_review) and failed
    sends, then classify. Reads the queue store under the DATA root through
    `action_queue.queue_path`, the same resolver the writers use; degrades
    to zero (not due) when the store is absent or unreadable.

    It COMPOSED that path here until 2026-09-20, from the four constants, for a
    reason this docstring gave as "a health probe must not materialise the
    directory it is asking about" -- which `queue_path`'s own `create=False`
    default already guarantees, so the second spelling bought nothing and cost
    the thing a second spelling always costs. On 2026-09-20 `queue_dir` learned
    to find a store the one-time mover never reached, and this line did not:
    the radar would have reported an empty queue while the CLI listed ten
    pending cards, which is worse than either answer alone."""
    qpath = queue_path(data_root)
    ready = failed = 0
    try:
        data = json.loads(qpath.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        # UnicodeError: the queue store is appended to by a live process, so a
        # torn write is the ordinary corruption, and `read_text` answers it with
        # UnicodeDecodeError -- a ValueError, caught by neither clause here.
        # `json.loads` never sees the bytes, so JSONDecodeError cannot stand in
        # for it. The docstring promises a degrade to zero; this raised into the
        # radar instead.
        return classify_queue(ready, failed)

    # Valid JSON of the WRONG SHAPE was not handled, and only OSError and
    # JSONDecodeError were caught. Five payloads written to the real queue path
    # each took the signal down with an uncaught exception: `[]`, `null` and
    # `"str"` raised AttributeError on `.get`; `{"actions": ["oops"]}` raised
    # AttributeError on a string card; `{"actions": null}` raised TypeError on
    # iteration. Measured 2026-08-26. This runs inside the ops radar, so one
    # malformed queue file took out every other signal beside it.
    actions = data.get("actions") if isinstance(data, dict) else None
    if not isinstance(actions, list):
        return classify_queue(ready, failed)
    for c in actions:
        if not isinstance(c, dict):
            continue
        status = c.get("status")
        if status == "send_failed":
            failed += 1
        # SENDING is in this set, and it is imported rather than typed, because
        # a claimed card belongs to neither branch above. A live claim lasts at
        # most the sender's 120 s, so counting it ready over-reports by one for
        # two minutes; a claim whose process DIED never leaves `sending` on its
        # own, and leaving it out meant a stranded send was invisible to the
        # radar forever. Over-reporting for two minutes beats silence for good.
        elif (status in ("pending", "approved", SENDING)
                and c.get("draft_status") == "ready_for_review"):
            ready += 1
    return classify_queue(ready, failed)


def odin_cadence_state(engine_root: Path) -> dict:
    """Run odin-cadence.py --json (reused compute), then classify.

    The run, the `returncode` check and the object-shape guard moved into
    `scripts/utils/odin_cadence.read_cadence_json` on 2026-08-29, unchanged in
    behaviour: `scripts/generate-dashboard.py` made the identical call and had
    neither guard, so a crashed helper reached its panel as a blank. This site
    ignores the reason string because the radar has always degraded to a
    not-due signal here and widening that is a separate decision.
    """
    cadence, _error = read_cadence_json(engine_root, timeout=60)
    return classify_odin(cadence)


# ============================================================
# Tier-A: ollama (probe)
# ============================================================

def classify_ollama(reachable: bool, model_present: bool | None,
                    model: str | None = None, *, auth_error: str = "") -> dict:
    """Pure: ollama reachability + embed-model presence -> signal dict (Tier A).

    `model` names the tag in the summary line. The CALLER supplies it, because
    this function is pure and reading the config here would end that. Omitted
    means the generic wording, which is right for a test that is asserting on
    reachability and has no opinion about the tag.

    `model_present=None` means NOT CHECKED, and is not the same as False. Since
    2026-08-23 the embed model is deliberately absent from the local daemon -
    embedding is pinned to the Windows side and `bge-m3` was removed here so the
    pin cannot be defeated by accident. A monitor that kept asserting its
    presence would report a permanent, false, unfixable failure; presence on the
    host that DOES need it is `ollama_accel_state`'s job.

    `auth_error` says the host's key variable is DECLARED and unset, which is a
    different failure from an unreachable host and sends the reader somewhere
    else entirely. It changes the summary only in the not-reachable branch,
    which is the one place it changes the repair; everywhere else it is carried
    in `value` and nothing is claimed from it. A host that answered is not
    diagnosed by a credential it did not need.
    """
    due = (not reachable) or (model_present is False)
    named = model or "the embed model"
    if not reachable and auth_error:
        severity, summary = "high", f"ollama: cannot authenticate -- {auth_error}"
    elif not reachable:
        severity, summary = "high", "ollama: unreachable"
    elif model_present is False:
        severity, summary = "high", f"ollama: up but {named} missing"
    else:
        severity, summary = "ok", "ollama: up"
    return {
        "key": "ollama",
        "value": {"reachable": reachable, "model_present": model_present,
                  "auth_error": auth_error},
        "threshold": None,
        "due": due,
        "severity": severity,
        "tier": "A",
        "summary": summary,
    }


def classify_ollama_accel(configured: bool, reachable: bool,
                          model_present: bool | None = None,
                          *, pin_unresolvable: bool = False,
                          auth_error: str = "") -> dict:
    """Pure: accelerated-host configuration + reachability -> signal dict (Tier B).

    Not configured is the normal state on most machines and is never due. A host
    that IS configured and does not answer is due at `high`, not `warn`: until
    2026-08-23 work continued on the local daemon and the cost was only speed,
    but embedding is now pinned here, so this host being down means recall and
    every rebuild stop. `model_present` is None when it could not be determined
    (host down, or the tags endpoint failed) and is never guessed.

    Tier B, not A, and deliberately. The accelerated daemon lives outside this
    OS (on a WSL2 workspace it is the Windows side), so nothing here can restart
    it; a Tier-A signal would wait for an auto-heal that cannot exist and stay
    invisible forever, since `select_candidates` only surfaces Tier A after two
    failed heals.

    `pin_unresolvable` says the operator DID pin a host and the pin names no
    usable address (a typo'd scheme, an `auto:` pin whose gateway cannot be
    read). It is configured, and it is not "down": nothing was ever probed, so
    saying a host failed to answer would assert what the method never
    established. It gets its own sentence for that reason.

    `auth_error` is that same reasoning one step along: the pin names a usable
    address, the address is key-gated, and the key variable it declares is
    unset. Nothing was established about the host either, because every probe
    went out unauthenticated and a 401 is not an answer about availability.
    "The pinned embed host is down" would send the operator to restart a server
    that is running.
    """
    due = configured and (not reachable or model_present is False)
    if not configured:
        severity, summary = "ok", "ollama-accel: not configured"
    elif pin_unresolvable:
        severity, summary = (
            "high",
            "ollama-accel: the pinned embed host is not a usable address "
            "-- nothing can embed",
        )
    elif not reachable and auth_error:
        severity, summary = (
            "high",
            f"ollama-accel: the pinned embed host's credential is unusable, so "
            f"nothing was established about it -- {auth_error}",
        )
    elif not reachable:
        # No longer "running on the local daemon": since 2026-08-23 embedding is
        # pinned here, so this host being down means nothing embeds at all.
        severity, summary = "high", "ollama-accel: pinned embed host down -- nothing can embed"
    elif model_present is False:
        severity, summary = "high", "ollama-accel: up but the embed model is not pulled there"
    else:
        severity, summary = "ok", "ollama-accel: up"
    return {
        "key": "ollama_accel",
        "value": {"configured": configured, "reachable": reachable,
                  "model_present": model_present,
                  "pin_unresolvable": pin_unresolvable,
                  "auth_error": auth_error},
        "threshold": None,
        "due": due,
        "severity": severity,
        "tier": "B",
        "summary": summary,
    }


def ollama_accel_state(engine_root: Path, timeout: int = 3) -> dict:
    """Probe the accelerated ollama host the memory index is configured to use.

    Reads the SAME preference the index reads, in the same order - `host` from
    `config/memory-index.yaml`, else the `HEADING_OS_INFERENCE_EMBED_HOST`
    environment variable, else `embed:` in the machine-local
    `config/inference-hosts.yaml` - so this reports on the endpoint that is
    actually used, not on a second opinion about which one it should be. A
    preference that resolves to the local daemon is not an accelerated host and
    counts as not configured.

    Keeping this list in step with `index_embed_target` is load-bearing: when the
    pin moved out of the tracked config on 2026-08-23, a monitor still reading
    only that file would have reported "not configured" on a machine that was
    pinned, and gone blind to exactly the outage it exists for.

    Scope: this is the index's EMBEDDING endpoint. Generation (`chronicle.py`,
    `census-submodel-bench.py`) resolves `generate:` from the same machine file
    and may point elsewhere; this signal says nothing about it.
    """
    from scripts.utils.embeddings import index_embed_preference
    from scripts.utils.inference_host import LOCAL_HOST, ProbeTarget, host_candidates

    # Asked of the index's own resolver rather than restated here. The three
    # sources and their order used to be spelled out a second time in this
    # function, and a second copy is the one that stops being fixed: on
    # 2026-09-06 the resolver learned to read a linked worktree's main checkout
    # and this monitor would not have, so it would have reported "not
    # configured" for every yard on a machine that is pinned - the precise
    # blindness the paragraph above says this must not have.
    preference = index_embed_preference(root=engine_root)

    # `host_candidates`, not `candidate_url`: since 2026-08-23 the pin may name
    # several ports on the same machine, and reading only the first entry would
    # call a healthy second one "not configured" - a monitor blind in exactly the
    # case the list was added for.
    resolved = host_candidates(preference)
    candidates = [c for c in resolved if c != LOCAL_HOST]
    if not candidates:
        # "Nothing is pinned" and "a host IS pinned and the pin names nothing
        # usable" both arrive here as an empty list, and until 2026-08-30 both
        # reported `not configured / ok / not due`. Measured that day with
        # `embed: "htp://typo"` in config/inference-hosts.yaml: this returned
        # severity ok while `resolve_pinned_host` refused the identical input
        # with InferenceHostUnavailable. The monitor showed green on precisely the
        # misconfiguration the resolver will not start on.
        #
        # `resolved`, not `candidates`, is what separates them: a preference that
        # resolves ONLY to the local daemon is a real, usable address and is
        # deliberately not an accelerated host, so it stays "not configured".
        if str(preference or "").strip() and not resolved:
            return classify_ollama_accel(True, False, pin_unresolvable=True)
        return classify_ollama_accel(False, False)

    # Armed before the first probe, and armed ONCE for the whole list: every
    # candidate here came from the EMBED pin, so they share one credential.
    # Until 2026-09-24 this probed unauthenticated, and MEASURED that day
    # against the live Lemonade it read a healthy server as down.
    headers, auth_error = _job_auth("embed", engine_root)
    for candidate in candidates:
        target = ProbeTarget(candidate, headers, auth_error)
        if target.reachable(timeout):
            return classify_ollama_accel(
                True, True,
                model_present=_embed_model_present(target, timeout,
                                                   root=engine_root),
            )
    return classify_ollama_accel(True, False, auth_error=auth_error)


def _job_auth(job: str, root: Path | None) -> tuple[dict, str]:
    """(headers, why-they-are-empty) for one job's host. NEVER raises.

    `embed_auth_headers` and `generation_auth_headers` both raise when the
    configuration DECLARES a key variable and nothing has set it. That is right
    for an embed call, which must not go out unauthenticated, and wrong here:
    everything in this module is read by monitors, and a monitor that raises
    takes the whole radar run with it -- the same shape as the `AttributeError`
    that escaped `_embed_model_present` on 2026-08-30.

    So the reason is CARRIED rather than dropped. "The key variable is not set"
    and "the host is down" are two repairs, and reporting the second for the
    first is how an operator ends up restarting a server that is running.
    `load_api_key` puts the variable NAME in its message and never the key, so
    the string returned here is safe to print and to put on the wire.
    """
    from scripts.utils.embeddings import embed_auth_headers
    from scripts.utils.inference_host import generation_auth_headers

    getter = embed_auth_headers if job == "embed" else generation_auth_headers
    try:
        return getter(root=root), ""
    except (ValueError, OSError) as exc:
        return {}, str(exc)


def unstartable_jobs(engine_root: Path | None = None) -> list[tuple[str, str, str]]:
    """(job, provider, host) for every job served by a program this host cannot start.

    Empty means every address in use is DECLARED as served by ollama, the one
    program a heal may legitimately try to start on this machine. Anything else
    is reported rather than attempted: Lemonade, today, for both jobs, which the
    operator starts himself outside WSL; and a job whose provider nobody
    declared, reported with provider "".

    DECLARED, never inferred from the address, for the reason
    `embeddings.index_embed_provider` gives: a resolver concluding "13305 means
    Lemonade" would be guessing, and would be wrong the first time the port
    moved. And declared in the strict sense `embeddings.declared_embed_provider`
    gives (HOS-215): until HOS-220 this read `index_embed_provider` and
    `generation_provider`, which fill a gap with "ollama", so on a machine that
    declared nothing the heal ran `systemctl` twice and spawned `ollama serve`,
    a program that is no longer installed here. A default a store needs in
    order to record a producer is not a licence to start a program.

    `host` is the unresolved preference as configured (a pin such as
    `auto:13305`, or "" when none), so the report can say WHERE to start it.
    """
    from scripts.utils.embeddings import declared_embed_provider, index_embed_preference
    from scripts.utils.inference_host import PROVIDER_OLLAMA, machine_hosts, machine_setting

    def _where(preference) -> str:
        if isinstance(preference, (list, tuple)):
            return ", ".join(str(h) for h in preference if h)
        return str(preference or "")

    jobs = (
        ("embed", declared_embed_provider(root=engine_root),
         _where(index_embed_preference(root=engine_root))),
        ("generate", machine_setting("generate_provider", root=engine_root),
         _where(machine_hosts("generate", root=engine_root))),
    )
    return [
        (job, provider, host)
        for job, provider, host in jobs
        if provider.strip().lower() != PROVIDER_OLLAMA
    ]


def _embed_model_present(target, timeout: int = 3, *,
                         root: Path | None = None) -> bool | None:
    """Whether the configured embed model is pulled on `target`. None if unknown.

    Asked of the PINNED host and nowhere else. Embedding happens there or it does
    not happen, so a missing tag there is a real outage - unlike its deliberate
    absence on the local daemon. None (the tags endpoint hiccuped) is reported as
    unknown rather than as missing: a monitor that guesses is worse than one that
    abstains.

    `root` is the tree whose config names the model, and it is the same tree the
    caller read the HOST out of. Without it the two halves of one answer came
    from two different clones - see `embeddings.index_embed_model`.

    `target` is a `ProbeTarget` rather than a bare address, so the credential
    arrives with the host it belongs to. Until 2026-09-24 this was a string and
    the request below went out unauthenticated: MEASURED that day, Lemonade
    answers 401 to `/api/tags` with no key, `json.JSONDecodeError`/`URLError`
    caught it, and the embed model on a host that HAS it was reported unknown.
    """
    from scripts.utils.embeddings import index_embed_model

    wanted = index_embed_model(root=root)
    # The host comes from config/inference-hosts.yaml, so its scheme is operator
    # input, not a literal. A `file:` host would make urlopen read a local path
    # and report its contents as an ollama tag list.
    tags_url = f"{target.url.rstrip('/')}/api/tags"
    if not tags_url.startswith(("http://", "https://")):
        return None
    try:
        request = urllib.request.Request(tags_url, headers=dict(target.headers))  # noqa: S310 - scheme guarded above
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310 - scheme guarded above
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None
    # A 200 carrying valid JSON that is not an OBJECT parses fine and then raised
    # AttributeError on `.get` below, which is in no except clause here: the
    # exception left `ollama_accel_state` and took the whole radar run with it,
    # exactly as the fixed `queue_state` defect did. Measured 2026-08-30 for
    # `null`, `[]` and `"x"`. `inference_host.probe` and `embeddings.model_digest`
    # both already guard the identical endpoint shape; this copy did not.
    # Unknown, never guessed: the caller reports it as "could not determine".
    if not isinstance(body, dict):
        return None
    for m in body.get("models", []) or []:
        name = (m.get("name") or m.get("model") or "") if isinstance(m, dict) else str(m)
        if name.startswith(wanted):
            return True
    return False


def inference_hosts_in_use(engine_root: Path | None = None) -> list["ProbeTarget"]:  # noqa: F821 - imported lazily inside, per this module's convention
    """Every address this machine expects a model server at, ARMED. Never empty.

    The union of the `embed` and `generate` pins, in that order, falling back to
    `OLLAMA_HOST` when the machine pins nothing. Probes nothing itself.

    Two pins rather than one because they are allowed to differ, and a monitor
    that watched only the embed host would miss a summarizer pointed elsewhere.

    It returned bare strings until 2026-09-24, and that is what made this the
    right place for the repair rather than one of its callers. Each pin declares
    its OWN key variable, so the credential is a property of the address and not
    of whoever is holding it: a caller handed a merged list of addresses from
    both jobs has no single header set it could correctly apply. Every consumer
    now receives `ProbeTarget`s and calls `.reachable()`, and a consumer written
    next month cannot repeat the omission by forgetting an argument. The
    credential is resolved per job and only for a job that actually named an
    address, so a machine pinning one job does not read the other's key.
    """
    from scripts.utils.embeddings import index_embed_preference
    from scripts.utils.inference_host import ProbeTarget, host_candidates, machine_hosts

    root = engine_root
    preferences = [
        ("embed", index_embed_preference(root=root)),
        ("generate", machine_hosts("generate", root=root)),
    ]
    seen: list[ProbeTarget] = []
    urls: set[str] = set()
    for job, preference in preferences:
        candidates = host_candidates(preference)
        if not candidates:
            continue
        headers, auth_error = _job_auth(job, root)
        for candidate in candidates:
            # First job to name an address owns it. Where both name the same
            # one it is the same server, so either credential reaches it.
            if candidate not in urls:
                urls.add(candidate)
                seen.append(ProbeTarget(candidate, headers, auth_error))
    return seen or [ProbeTarget(OLLAMA_HOST)]


def ollama_state(host: str | None = None, timeout: int = 3) -> dict:
    """Is an ollama answering for this workspace at all? Tier A.

    Read-only HTTP GET to /api/version against each address in
    `inference_hosts_in_use()`; the first to answer makes the signal green.
    Unreachable everywhere -> due, and Tier-A heal tries to start one. `host` is
    injectable for tests (point at a dead port to exercise the unreachable path).

    It watched only localhost until 2026-08-23, when the local ollama was removed.

    It still does not check for the embed model: presence of the model where it
    IS required is `ollama_accel_state`'s job, on the host that must have it.

    Two things changed on 2026-09-24, and they are one change. The addresses now
    arrive ARMED from `inference_hosts_in_use`, because MEASURED that day this
    probed a key-gated Lemonade unauthenticated, received 401, caught it as a
    `URLError`, and reported a live server as unreachable -- which made
    `ops-radar heal` skip a memory-index rebuild that would have succeeded. And
    the hand-rolled request this function carried is gone in favour of
    `ProbeTarget.reachable`, which is `inference_host.probe`: the copy here checked
    only that the body parsed as JSON, while `probe` also requires a mapping
    carrying `version`, the guard written after a loopback server answering
    `null` was read as a healthy ollama on 2026-08-25. Two spellings of one
    request is the shape this whole repair is about.
    """
    from scripts.utils.inference_host import ProbeTarget

    # A bare string stays accepted: the argument exists so a test can point this
    # at a dead port, and such a host carries no credential by construction.
    targets = [ProbeTarget(host)] if host else inference_hosts_in_use()
    reachable = any(target.reachable(timeout) for target in targets)
    auth_error = next((t.auth_error for t in targets if t.auth_error), "")
    return classify_ollama(reachable, None, auth_error=auth_error)


# ============================================================
# Tier-A: memory-index freshness (fs)
# ============================================================

def classify_index(build_age_days: int | None, sources_newer: bool) -> dict:
    """Pure: index build age + whether sources are newer than the last build.

    None build age means the index was never built (due, high). Otherwise due
    when sources changed since the last build OR the build is older than
    INDEX_STALE_DAYS.
    """
    if build_age_days is None:
        return {
            "key": "memory_index",
            "value": "absent",
            "threshold": INDEX_STALE_DAYS,
            "due": True,
            "severity": "high",
            "tier": "A",
            "summary": "memory-index: never built",
        }
    due = sources_newer or build_age_days >= INDEX_STALE_DAYS
    if sources_newer or build_age_days >= INDEX_STALE_DAYS * 3:
        severity = "high"
    elif due:
        severity = "warn"
    else:
        severity = "ok"
    return {
        "key": "memory_index",
        "value": {"build_age_days": build_age_days, "sources_newer": sources_newer},
        "threshold": INDEX_STALE_DAYS,
        "due": due,
        "severity": severity,
        "tier": "A",
        "summary": (
            f"memory-index: {build_age_days}d old"
            + (", sources newer" if sources_newer else "")
        ),
    }


# Fallback source dirs, used ONLY when the indexer's own config cannot be read.
#
# These three were the whole watch list, hand-written beside an indexer that
# ingests fourteen layers. Everything else it indexes went unwatched: a new CRM
# contact, a new auto-memory, a new deliverable under outputs/, a new reference
# file, a new plan, a new linkedin archive entry, a new datastore extract, a new
# chronicle entry, a new skill or rule. Each was indexed and none of them could
# ever make this signal say the index was stale. Measured 2026-08-26 against a
# synthetic data root: four files written after the build (crm/contacts/,  # leak-guard: ok (measurement: the address as it was on the day measured)
# auto-memory/, outputs/research/, reference/) left `sources_newer` False and  # leak-guard: ok (measurement: the address as it was on the day measured)
# `severity` ok; one file under knowledge/ flipped it. The watch list is now
# DERIVED from the same config the builder reads, so a layer added there is
# watched here without anyone remembering to.
#
# The data half of the fallback is registry names, resolved per call: the
# knowledge base, the `business-threads` group and the `context-homes` group.
_FALLBACK_DATA_NAMES = ("knowledge-base", "business-threads", "context-homes")
_FALLBACK_ENGINE_DIRS = (".claude/skills", ".claude/rules")


def _fallback_data_dirs(data_root: Path) -> list[Path]:
    """The absolute directories `_FALLBACK_DATA_NAMES` name on this data root."""
    try:
        return [data_path("knowledge-base", data_root=data_root),
                *homes("business-threads", data_root=data_root),
                *homes("context-homes", data_root=data_root)]
    except DataPathError as exc:
        print(f"[ops_signals] the fallback watch list could not be resolved ({exc}); "
              f"no data directory is watched", file=sys.stderr)
        return []

_INDEX_CONFIG_REL = "config/memory-index.yaml"


def _expand_braces(pattern: str) -> list[str]:
    """Expand one `{a,b}` group at a time. pathlib globbing has no braces."""
    start = pattern.find("{")
    if start == -1:
        return [pattern]
    end = pattern.find("}", start)
    if end == -1:
        return [pattern]
    pre, body, post = pattern[:start], pattern[start + 1:end], pattern[end + 1:]
    out = []
    for option in body.split(","):
        out.extend(_expand_braces(pre + option.strip() + post))
    return out


def _index_source_globs(engine_root: Path, data_root: Path | None = None) -> list[str] | None:
    """Every `glob:` the indexer's layer config declares, braces expanded.

    None when the config cannot be read or declares no globs - the callers below
    then fall back to the hand-written dirs AND say they narrowed, rather than
    reporting a full sweep they did not run.

    "Cannot be read" includes PARSES BUT IS THE WRONG SHAPE, since 2026-08-30.
    This file is operator-editable, and only `OSError` and `YAMLError` were
    caught: a config that parses to a list raised AttributeError on `cfg.get`,
    and a `layers:` holding strings (or a mapping, whose iteration yields string
    keys) raised the same on `layer.get`. Measured that day for all three. The
    docstring promised a fall back and the radar got a crash instead - the same
    valid-JSON-wrong-shape class already fixed in `queue_state` and
    `_read_trend_records`.
    """
    path = Path(engine_root) / _INDEX_CONFIG_REL
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError):
        # UnicodeError, same reason as `queue_state` above: the decode happens
        # in `read_text`, before PyYAML is handed anything, so a config with an
        # undecodable byte raised UnicodeDecodeError past both named clauses.
        return None
    if not isinstance(cfg, dict):
        return None
    layers = cfg.get("layers") or []
    if not isinstance(layers, list):
        return None
    # A registry-named layer carries its directories as names; resolve them the
    # way the indexer does, or its glob is relative to a directory not given.
    layers = resolve_layers(layers, data_root)
    patterns: list[str] = []
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        # A commit corpus layer carries `source: git-log` and no glob; the
        # builder reads git, so there is no file mtime to compare against.
        glob = layer.get("glob")
        if isinstance(glob, str) and glob:
            patterns.extend(_expand_braces(glob))
    return patterns or None


def _newest_mtime(base: Path, rel_dirs) -> float | None:
    newest = None
    for rel in rel_dirs:
        d = base / rel
        if not d.is_dir():
            continue
        for p in d.rglob("*.md"):
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            if newest is None or mt > newest:
                newest = mt
    return newest


def _newest_by_glob(base: Path, patterns) -> float | None:
    """Newest mtime among the files the indexer would actually ingest."""
    newest = None
    base = Path(base)
    for pattern in patterns:
        try:
            matches = base.glob(pattern)
        except (OSError, ValueError):
            continue
        for p in matches:
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            if newest is None or mt > newest:
                newest = mt
    return newest


def index_freshness_state(engine_root: Path, data_root: Path, now: float | None = None) -> dict:
    """Compare newest indexed source vs the last successful build (index.db mtime).

    The content index is runtime-junk/memory-index/cache/index.db under the DATA
    root (`memory-index.py`'s `content_store_path`); its mtime is the proxy for
    the last successful build (the build writes it on success and fails loud
    otherwise). Sources newer than that mtime -> the index is stale. Spelled
    through `runtime_root`, which creates nothing: a health probe must not
    materialise the directory it is asking about.
    """
    now = time.time() if now is None else now
    index_db = runtime_root(data_root) / "memory-index" / "cache" / "index.db"
    try:
        build_mtime = index_db.stat().st_mtime
    except OSError:
        return classify_index(None, False)
    build_age_days = int((now - build_mtime) // 86400)
    patterns = _index_source_globs(engine_root, data_root)
    if patterns is None:
        # Say what was NOT swept. A narrowed check that prints like a complete
        # one is the defect this whole function was rewritten for.
        print(f"[ops_signals] {_INDEX_CONFIG_REL} unreadable; the staleness check "
              f"covers only {', '.join(_FALLBACK_DATA_NAMES + _FALLBACK_ENGINE_DIRS)}",
              file=sys.stderr)
        newest_data = _newest_mtime(data_root, _fallback_data_dirs(data_root))
        newest_engine = _newest_mtime(engine_root, _FALLBACK_ENGINE_DIRS)
    else:
        # Each glob is tried against BOTH roots: the config does not say which
        # store a layer lives in, and a pattern that matches nothing under one
        # root simply yields nothing.
        newest_data = _newest_by_glob(data_root, patterns)
        newest_engine = _newest_by_glob(engine_root, patterns)
    newest_source = max((m for m in (newest_data, newest_engine) if m is not None), default=None)
    sources_newer = newest_source is not None and newest_source > build_mtime
    return classify_index(build_age_days, sources_newer)


# ============================================================
# Router accuracy (F-6.2) - Tier B
# ============================================================

# Point thresholds (rates are 0-1 fractions, scaled *100 to points like eval-drift).
ROUTER_ACCURACY_DROP_PCT = 10.0    # a skill dropping > this many points vs baseline is due (warn)
ROUTER_ACCURACY_HIGH_PCT = 20.0    # ... a bigger single-skill drop escalates to high
ROUTER_ACCURACY_BASELINE_N = 7     # rolling-baseline window (prior records), mirrors eval-drift


def classify_router_accuracy(latest: dict | None, baseline: dict | None) -> dict:
    """Pure: compare the latest router-accuracy record against a rolling baseline.

    `latest` / `baseline` are record-shaped dicts {overall_rate, per_skill:{name:rate}}
    with rates as 0-1 fractions; `baseline` is the per-skill mean of the prior window.
    Point-scaled like eval-drift: drop_pts = (baseline_rate - latest_rate) * 100. due
    when any skill dropped > ROUTER_ACCURACY_DROP_PCT points OR the aggregate overall_rate
    dropped > that; a single skill dropping > ROUTER_ACCURACY_HIGH_PCT or an aggregate drop
    escalates to high. Not due when there is no baseline (< 2 records). Tier B - a sovereign
    manual nudge (the CEO investigates a routing regression), never machine-auto-healable."""
    worst_skill = None
    worst_drop = 0.0
    overall_drop = 0.0
    if latest and baseline:
        lp = latest.get("per_skill") or {}
        bp = baseline.get("per_skill") or {}
        for name, brate in bp.items():
            lrate = lp.get(name)
            if brate is not None and lrate is not None:
                drop = (brate - lrate) * 100.0
                if drop > worst_drop:
                    worst_drop = drop
                    worst_skill = name
        lo = latest.get("overall_rate")
        bo = baseline.get("overall_rate")
        if lo is not None and bo is not None:
            overall_drop = (bo - lo) * 100.0

    # No measurement at all is a reason to raise, not a reason to say ok. Measured
    # 2026-08-03: this returned `due=False, severity="ok"` while the job producing
    # the trend had never run once on any host, so the Tier-B alert described as
    # "waiting on output" reported healthy for the whole of that time. A signal
    # whose absence of data reads as good news cannot detect the failure that
    # matters most, which is the producer being dead. A PRESENT latest with no
    # baseline stays not-due: that is a trend legitimately forming.
    if latest is None:
        return {
            "key": "router_accuracy",
            "value": {"worst_skill": None, "worst_drop_pts": 0.0, "overall_drop_pts": 0.0},
            "threshold": ROUTER_ACCURACY_DROP_PCT,
            "due": True,
            "severity": "warn",
            "tier": "B",
            "summary": ("router-accuracy: no measurement on record - the nightly "
                        "run is not producing data"),
        }

    due = worst_drop > ROUTER_ACCURACY_DROP_PCT or overall_drop > ROUTER_ACCURACY_DROP_PCT
    if worst_drop > ROUTER_ACCURACY_HIGH_PCT or overall_drop > ROUTER_ACCURACY_DROP_PCT:
        severity = "high"
    elif due:
        severity = "warn"
    else:
        severity = "ok"

    if worst_skill:
        summary = (
            f"router-accuracy: {worst_skill} -{worst_drop:.0f}pt vs baseline"
            + (f", overall -{overall_drop:.0f}pt" if overall_drop > 0 else "")
        )
    elif baseline is None:
        summary = "router-accuracy: baseline forming (no comparable prior run)"
    else:
        summary = "router-accuracy: stable"

    return {
        "key": "router_accuracy",
        "value": {
            "worst_skill": worst_skill,
            "worst_drop_pts": round(worst_drop, 1),
            "overall_drop_pts": round(overall_drop, 1),
        },
        "threshold": ROUTER_ACCURACY_DROP_PCT,
        "due": due,
        "severity": severity,
        "tier": "B",
        "summary": summary,
    }


def _read_trend_records(trend_path: Path, limit: int) -> list[dict]:
    """Return up to the last `limit` parsed JSONL OBJECTS; [] if absent/unreadable.

    Non-object lines are dropped. `123` and `"abc"` are valid JSON, so they used
    to survive the JSONDecodeError filter and reach every `.get` downstream:
    `router_accuracy_state` raised AttributeError on an int. Measured
    2026-08-26. A trend file is appended to by a nightly job, and a truncated or
    interleaved write is exactly how a stray scalar line gets there.
    """
    try:
        lines = trend_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        # UnicodeError, for the reason the paragraph above already establishes
        # about this very file: it is appended to by a nightly job, so a
        # truncated or interleaved write is the expected corruption. Half a
        # multi-byte character is that corruption, and `read_text` answers it
        # with UnicodeDecodeError -- a ValueError, not an OSError -- out of a
        # reader whose whole job is to survive a bad line.
        return []
    records: list[dict] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def router_accuracy_state(data_root: Path) -> dict:
    """Read the router-accuracy trend under the DATA root, build a rolling baseline
    (per-skill mean of the prior up-to-N records MEASURED BY THE SAME JUDGE MODEL),
    and classify. An ABSENT trend is due at warn, not quiet: `classify_router_accuracy`
    treats no measurement on record as the producer being dead, which is the failure
    that matters most, and this sentence claimed the opposite until 2026-08-30. A
    trend that exists but has < 2 records, or carries no prior run on the current
    judge, is the not-due case (a baseline legitimately forming). The trend lives
    at the `router-accuracy-trend` address under the data root, written by
    router-accuracy-nightly.py."""
    # The READ half of a pair. `router-accuracy-nightly.out_dir()` is the write
    # half and asks the registry for this same `router-accuracy-trend` row, so
    # there is one address and nothing for the two halves to disagree about.
    # They must not: an absent trend is classified here as the producer being
    # dead, so a reader looking at the wrong directory would raise a nightly
    # alarm about a producer that was writing happily somewhere else. MEASURED
    # 2026-09-22 on the operator's overlay, the registry name and the migration
    # seam it replaced resolve to the identical absolute path.
    trend_path = data_path("router-accuracy-trend", data_root=data_root)
    records = _read_trend_records(trend_path, ROUTER_ACCURACY_BASELINE_N + 1)
    # A refusal shares the file with the measurements so a silent night is
    # visible, which is the whole point of writing it. It carries no `per_skill`,
    # so counting it as a data point would rebuild the sibling daemon's 0/0
    # baseline in a new place: a trend of pure refusals must read as no data.
    records = [r for r in records if r.get("status") != "refused"]
    if len(records) < 2:
        return classify_router_accuracy(records[-1] if records else None, None)
    latest = records[-1]
    # Compare like with like: the judge is the measuring instrument, and the harness
    # resolves a model FAMILY, so the instrument replaces itself without anyone
    # touching the router. Measured 2026-08-10, the night the Sonnet family advanced
    # a release: 32 of 69 skills "dropped", almost all by exactly one case, and the
    # Tier-B alert named /voss at -38pt while no commit had touched .claude/skills or
    # .claude/rules for two days. A baseline built across a model change measures the
    # models, not the routing.
    prior = [r for r in records[:-1] if r.get("model") == latest.get("model")]
    if not prior:
        return classify_router_accuracy(latest, None)
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    overall_sum = 0.0
    overall_n = 0
    for rec in prior:
        ov = rec.get("overall_rate")
        if ov is not None:
            overall_sum += ov
            overall_n += 1
        for name, rate in (rec.get("per_skill") or {}).items():
            if rate is not None:
                sums[name] = sums.get(name, 0.0) + rate
                counts[name] = counts.get(name, 0) + 1
    baseline = {
        "overall_rate": (overall_sum / overall_n) if overall_n else None,
        "per_skill": {name: sums[name] / counts[name] for name in sums},
    }
    return classify_router_accuracy(latest, baseline)


# ============================================================
# Tier-B: daemon restart loops and a stale Exchange sync
# ============================================================
#
# Added 2026-10-12. MEASURED that day: sync-exchange-daemon restart-looped for
# about two days (20,261 restarts) on a PID file whose number now belonged to
# another process, and nothing said so. A looping unit reads `active` between
# two crashes, so a liveness check passes it; only the restart count, or the
# work it no longer does, shows the loop.

def classify_restart_loops(counts: dict[str, int] | None,
                           window_hours: int = RESTART_LOOP_WINDOW_HOURS) -> dict:
    """Pure: automatic restarts per user unit in the window -> signal dict.

    `counts` None means the journal could not be read: not due, and the summary
    says the check did not run rather than reporting a clean fleet.
    """
    if counts is None:
        return {"key": "restart_loop", "value": None, "threshold": RESTART_LOOP_MAX,
                "due": False, "severity": "ok", "tier": "B",
                "summary": "restart loops: not checked (user journal unreadable)"}
    looping = {u: n for u, n in counts.items() if n > RESTART_LOOP_MAX}
    worst = sorted(looping.items(), key=lambda kv: kv[1], reverse=True)
    summary = ("restart loop: " + ", ".join(f"{u} x{n} in {window_hours}h" for u, n in worst)
               if worst else "restart loops: none")
    return {"key": "restart_loop", "value": looping, "threshold": RESTART_LOOP_MAX,
            "due": bool(looping), "severity": "high" if looping else "ok", "tier": "B",
            "summary": summary}


def restart_loop_state(window_hours: int = RESTART_LOOP_WINDOW_HOURS) -> dict:
    """Count systemd's own "Scheduled restart job" lines per USER unit in the
    window, from the user journal, and classify.

    The journal rather than `NRestarts`: the counter resets on every manual
    start and holds no time, so a rise over a window needs samples somebody
    stores, while the journal already has each restart with its timestamp.
    Read-only. MEASURED 2026-10-12 over 2026-10-10 00:00 to 2026-10-12 03:00:
    11,222 lines for sync-exchange-daemon.service.
    """
    try:
        proc = subprocess.run(
            ["journalctl", "--user", "--since", f"-{int(window_hours)}h", "--no-pager",
             "-o", "cat", "-g", "Scheduled restart job"],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return classify_restart_loops(None, window_hours)
    # journalctl exits 1 when the pattern matched nothing; that is a clean answer.
    if proc.returncode not in (0, 1):
        return classify_restart_loops(None, window_hours)
    counts: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        unit, sep, _ = line.partition(": Scheduled restart job")
        if sep and unit:
            counts[unit] = counts.get(unit, 0) + 1
    return classify_restart_loops(counts, window_hours)


def classify_exchange_sync(age_hours: float | None, unit_present: bool) -> dict:
    """Pure: hours since the last successful Exchange sync -> signal dict.

    No unit and no log line means this machine does not run the sync: not due.
    A unit with no successful sync on record is due at high.
    """
    if not unit_present and age_hours is None:
        return {"key": "exchange_sync", "value": None, "threshold": EXCHANGE_SYNC_STALE_HOURS,
                "due": False, "severity": "ok", "tier": "B",
                "summary": "exchange sync: not run on this machine"}
    if age_hours is None:
        return {"key": "exchange_sync", "value": None, "threshold": EXCHANGE_SYNC_STALE_HOURS,
                "due": True, "severity": "high", "tier": "B",
                "summary": "exchange sync: no successful sync on record"}
    due = age_hours > EXCHANGE_SYNC_STALE_HOURS
    severity = ("high" if age_hours > EXCHANGE_SYNC_HIGH_HOURS
                else "warn" if due else "ok")
    return {"key": "exchange_sync", "value": round(age_hours, 1),
            "threshold": EXCHANGE_SYNC_STALE_HOURS, "due": due, "severity": severity,
            "tier": "B", "summary": f"exchange sync: last success {age_hours:.0f}h ago"}


def exchange_sync_state(now: float | None = None) -> dict:
    """Age of the last 'job-ok sync-exchange' in the daemon's log, read by the
    same parser the pulse uses, then classify. Read-only."""
    from scripts.utils.data_path import DataPathError
    from scripts.utils.sync_exchange_paths import daemon_files, last_job_ok
    try:
        unit = subprocess.run(
            ["systemctl", "--user", "show", "-p", "LoadState", "--value",
             "sync-exchange-daemon.service"], capture_output=True, text=True, timeout=10)
        unit_present = unit.stdout.strip() == "loaded"
    except (OSError, subprocess.SubprocessError):
        unit_present = False
    try:
        last = last_job_ok(daemon_files(create=False).log)
    except DataPathError:
        last = None
    now = time.time() if now is None else now
    age = None if last is None else max(0.0, (now - last.timestamp()) / 3600)
    return classify_exchange_sync(age, unit_present)


def classify_unit_drift(drifted: list[str] | None) -> dict:
    """Pure: installed user units whose DIRECTIVES differ from their template -> signal dict.

    `drifted` None means the survey could not run: not due, and the summary says so.
    A drifted unit is re-rendered by re-running its installer (engine audit K3-1,
    2026-10-16: a unit installed before its template gained `Environment=TZ=`
    ran on the host zone with nothing reporting it).
    """
    if drifted is None:
        return {"key": "unit_drift", "value": None, "threshold": 0, "due": False,
                "severity": "ok", "tier": "B",
                "summary": "unit drift: not checked (survey failed)"}
    summary = ("unit drift: " + ", ".join(sorted(drifted)) + " (re-run the installer)"
               if drifted else "unit drift: none")
    return {"key": "unit_drift", "value": sorted(drifted), "threshold": 0,
            "due": bool(drifted), "severity": "warn" if drifted else "ok",
            "tier": "B", "summary": summary}


def unit_drift_state(engine_root: Path) -> dict:
    """Survey the installed user units against their templates through
    `scripts/check-unit-drift.py` (loaded by path, its own logic), and classify.
    Read-only."""
    import importlib.util
    try:
        spec = importlib.util.spec_from_file_location(
            "_ops_unit_drift", engine_root / "scripts" / "check-unit-drift.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        result = mod.survey(mod.default_units_dir(), engine_root / "scripts" / "templates" / "systemd")
    except Exception as exc:  # noqa: BLE001 - any failure is reported as "not checked"
        print(f"ops_signals: unit drift survey failed: {exc}", file=sys.stderr)
        return classify_unit_drift(None)
    if result.get("errors"):
        return classify_unit_drift(None)
    return classify_unit_drift([r["unit"]
                                for r in result["results"]
                                if r.get("verdict") == "directive-drift"])


# ============================================================
# Tier-B: a hostname pinned in /etc/hosts that public DNS no longer answers
# ============================================================
#
# Added 2026-10-25. A local resolver answered an unreachable address for a host
# the workspace depends on, and the operator pinned the host to its public
# address with one /etc/hosts line. A pin hard-codes an address: if the server
# moves, the pin keeps sending traffic to the old one and nothing says why. This
# signal compares each configured pin with what a public resolver answers.
#
# The hostnames to watch are private and live only in the data repository, at
# the registry name `hosts-pins`: a YAML mapping with one key, `hosts`, a list of
# hostnames. No name, no file, or an empty list: nothing is checked and the
# signal is not due. The public answer comes from DNS over HTTPS (RFC 8484 JSON
# form) through urllib with TLS verification on, so a broken local resolver
# cannot vouch for itself.

HOSTS_FILE = Path("/etc/hosts")
HOSTS_PIN_DOH_URL = "https://1.1.1.1/dns-query"
HOSTS_PIN_TIMEOUT = 5
HOSTS_PIN_MAX_CNAME = 8


def read_hosts_pins(hosts_file: Path = HOSTS_FILE) -> dict[str, list[str]]:
    """Map each hostname in a hosts file to its pinned IPv4 addresses (lowercase names)."""
    import ipaddress
    pins: dict[str, list[str]] = {}
    for line in hosts_file.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = line.split("#", 1)[0].split()
        if len(fields) < 2:
            continue
        try:
            ip = str(ipaddress.IPv4Address(fields[0]))
        except ValueError:
            continue
        for name in fields[1:]:
            pins.setdefault(name.lower().rstrip("."), []).append(ip)
    return pins


def load_pinned_hostnames(data_root: Path | None = None) -> list[str]:
    """The private list of hostnames whose pins are checked; [] when none is configured."""
    try:
        path = data_path("hosts-pins", data_root=data_root)
    except DataPathError:
        return []
    if not path.is_file():
        return []
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        print(f"ops_signals: {path.name} unreadable: {exc}; nothing checked", file=sys.stderr)
        return []
    hosts = doc.get("hosts") if isinstance(doc, dict) else None
    if not isinstance(hosts, list):
        print(f"ops_signals: {path.name}: expected a mapping with a `hosts` list; nothing checked",
              file=sys.stderr)
        return []
    return [str(h).strip().lower().rstrip(".") for h in hosts if str(h).strip()]


def public_a_records(hostname: str, url: str = HOSTS_PIN_DOH_URL,
                     timeout: int = HOSTS_PIN_TIMEOUT) -> list[str] | None:
    """IPv4 answers for `hostname` from a public DoH resolver, following CNAMEs.

    Returns None when the resolver did not answer usably (network, timeout, HTTP
    error, SERVFAIL, malformed reply, a CNAME loop): that is NOT EVALUATED, never
    drift. NXDOMAIN is an answer, and returns [].
    """
    import http.client
    import urllib.parse
    from scripts.utils.user_agent import UserAgentConfigError, user_agent
    if not url.startswith("https://"):
        raise ValueError(f"public_a_records: resolver URL must be https, got {url!r}")
    name = hostname
    for _ in range(HOSTS_PIN_MAX_CNAME):
        query = urllib.parse.urlencode({"name": name, "type": "A"})
        try:
            req = urllib.request.Request(f"{url}?{query}",  # noqa: S310 - https enforced above
                                         headers={"Accept": "application/dns-json",
                                                  "User-Agent": user_agent()})
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - https enforced above
                reply = json.loads(resp.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError,
                UserAgentConfigError) as exc:
            print(f"ops_signals: public DNS lookup for {hostname} failed: {exc}", file=sys.stderr)
            return None
        status = reply.get("Status") if isinstance(reply, dict) else None
        if status == 3:
            return []
        if status != 0:
            print(f"ops_signals: public DNS lookup for {hostname} returned status {status}",
                  file=sys.stderr)
            return None
        raw = reply.get("Answer") or []
        if not isinstance(raw, list):
            print(f"ops_signals: public DNS lookup for {hostname}: malformed Answer", file=sys.stderr)
            return None
        answers = [a for a in raw if isinstance(a, dict)]
        addrs = [str(a.get("data")) for a in answers if a.get("type") == 1]
        if addrs:
            return sorted(set(addrs))
        cnames = [str(a.get("data", "")).rstrip(".") for a in answers if a.get("type") == 5]
        if not cnames:
            return []
        name = cnames[-1]
    print(f"ops_signals: public DNS lookup for {hostname}: CNAME chain longer than "
          f"{HOSTS_PIN_MAX_CNAME}", file=sys.stderr)
    return None


def classify_hosts_pins(results: list[dict]) -> dict:
    """Pure: one result per configured host -> signal dict.

    Each result is {host, pinned: [ip] | [], public: [ip] | None}. Drift is a
    pinned host none of whose pinned addresses is among the public answers. A host
    with no hosts line is noted as not pinned; a host the resolver did not answer
    for is noted as NOT EVALUATED. Neither is due.
    """
    drift, notes = [], []
    for r in results:
        if not r["pinned"]:
            notes.append(f"hosts pin: {r['host']} not pinned in /etc/hosts")
        elif r["public"] is None:
            notes.append(f"hosts pin: {r['host']} NOT EVALUATED (public DNS did not answer)")
        elif not set(r["pinned"]) & set(r["public"]):
            drift.append(r)
    if drift:
        parts = [f"{r['host']} pinned to {', '.join(r['pinned'])}, public DNS answers "
                 f"{', '.join(r['public']) or 'nothing (NXDOMAIN)'}" for r in drift]
        summary = ("hosts pin: " + "; ".join(parts)
                   + " - update or remove the /etc/hosts line with sudo in a real terminal")
    elif results:
        checked = sum(1 for r in results if r["pinned"] and r["public"] is not None)
        summary = (f"hosts pin: {checked} of {len(results)} configured host(s) checked, "
                   "no drift found")
    else:
        summary = "hosts pin: none configured"
    return {"key": "hosts_pin", "value": [r["host"] for r in drift], "threshold": 0,
            "due": bool(drift), "severity": "warn" if drift else "ok", "tier": "B",
            "summary": summary, "notes": notes}


def hosts_pin_state(data_root: Path | None = None, hosts_file: Path = HOSTS_FILE,
                    resolve=public_a_records, hostnames: list[str] | None = None) -> dict:
    """Read the configured hostnames and the hosts file, ask public DNS, classify. Read-only."""
    if hostnames is None:
        hostnames = load_pinned_hostnames(data_root)
    if not hostnames:
        return classify_hosts_pins([])
    try:
        pins = read_hosts_pins(hosts_file)
    except OSError as exc:
        print(f"ops_signals: cannot read {hosts_file}: {exc}", file=sys.stderr)
        sig = classify_hosts_pins([])
        sig["summary"] = f"hosts pin: NOT EVALUATED ({hosts_file} unreadable)"
        sig["notes"] = [sig["summary"]]
        return sig
    results = []
    for host in hostnames:
        pinned = pins.get(host, [])
        results.append({"host": host, "pinned": pinned,
                        "public": resolve(host) if pinned else None})
    return classify_hosts_pins(results)
