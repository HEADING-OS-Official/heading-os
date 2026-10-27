#!/usr/bin/env python3
"""SessionStart hook: surface urgent CRM contacts and data freshness alerts."""
import sys
import json
import os
import re
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

def _load_engine_module(relpath, label, importer, consequence):
    """The walk-and-import shape for an OPTIONAL engine helper.

    `consequence` is what the operator loses when the import fails, and it is
    a parameter rather than a generic sentence: "unavailable" without a cost is
    a line people learn to scroll past.
    """
    here = Path(__file__).resolve()
    for candidate in [here.parent, *here.parents]:
        if (candidate / relpath).is_file():
            sys.path.insert(0, str(candidate))
            try:
                return importer()
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                print(f"session-start: {label} unavailable ({exc}); "
                      f"{consequence}", file=sys.stderr)
                return None
    print(f"session-start: no {relpath} on this clone; {consequence}",
          file=sys.stderr)
    return None


# ============================================================
# The hook's internal time budget
# ============================================================
#
# Claude Code DISCARDS the output of a hook that outruns its REGISTERED timeout,
# and here the loss is everything this file computes: CRM red debt, stale
# context and the thread panel, with exit 0 and no alerts, which reads as a
# healthy workspace.
#
# `.claude/settings.json` registers this hook at 15 seconds. The budget inside
# it is the CRM child (CRM_HEALTH_TIMEOUT_SECONDS, 8) plus a tail of about 4 s
# for the staleness scan, the thread panel, the print and the launcher (the tail
# MEASURED 0.23 s on 2026-08-31): 8 + 4 = 12, three seconds inside the wall.
# Change this number and the registration together; no check holds them since
# the 2026-09-21 test-suite split.
CRM_HEALTH_TIMEOUT_SECONDS = 8


_CRM_CACHE_TTL_SECONDS = 1800  # 30 minutes


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _red_contacts(output: str) -> list:
    """The overdue CONTACT lines from crm-health.py output, not its header.

    The filter was `"RED" in line`, which matches the section HEADER
    `RED - Overdue` and nothing else: the contact lines below it carry a name, a
    company and a day count, never the word RED. So `len()` of the result was
    the number of headers, always 1, and the session banner read "CRM ALERT: 1
    contact(s) need attention today" whether one contact was overdue or forty.
    Measured 2026-08-23 against a three-overdue fixture: the hook reported 1.

    The substring was also unanchored, so a contact or company containing RED
    (REDACTED, REDMOND, a person called FRED) counted as an alert.

    Read the section instead: everything indented under the RED header, up to
    the blank line that ends it. ANSI codes are stripped, because crm-health.py
    colourizes unconditionally and the cached strings are shown to a human.
    """
    lines = _ANSI_RE.sub("", output).split("\n")
    contacts = []
    inside = False
    for line in lines:
        stripped = line.strip()
        if not inside:
            if stripped.startswith("RED - "):
                inside = True
            continue
        if not stripped:
            break  # blank line ends the section
        if not line.startswith((" ", "\t")):
            break  # next section header, unindented
        contacts.append(stripped)
    return contacts


def _crm_cache_file(project_dir):
    """Where the CRM red-debt cache lives: this checkout's hook state, or None.

    It holds contact lines, so it lives in the data repository beside the rest
    of this checkout's hook state (registry name `checkout-state`), never in
    the engine checkout, where an ignored file is invisible to every wall that
    guards the public tree. It sat in `.sessions/` of the checkout until
    2026-09-28. A tree with no data repository gets `.claude/state/` from
    `checkout_state_root`, and has no CRM for the cache to hold. None when the
    address cannot be resolved; the check then runs uncached rather than
    writing somewhere unguarded.
    """
    if project_dir not in sys.path:
        sys.path.insert(0, project_dir)
    try:
        from scripts.utils.checkpoint_paths import checkout_state_root

        root = checkout_state_root(Path(project_dir))
    except Exception as exc:  # noqa: BLE001 - reported; the check still runs
        print(f"[session-start] crm-health cache address unavailable ({exc}); "
              f"running uncached", file=sys.stderr)
        return None
    return str(root / "crm-health-cache.json")


def check_crm_health(project_dir):
    """Run CRM health check and extract RED contacts. Result cached for 30 minutes
    in this checkout's hook state (`_crm_cache_file`) to keep SessionStart fast.

    Returns `(red_contacts, failure)`. `failure` is None on every path where the
    check actually ran, and a one-line reason on the path where it did not.

    It used to return one list carrying both meanings, and `main()` read only
    `len()` of it: a crm-health.py that exited non-zero produced the banner
    `CRM ALERT: 1 contact(s) need attention today`, byte-identical to a session
    with exactly one genuinely overdue contact. The failure string this function
    took care to write reached nothing but stderr, which an exit-0 SessionStart
    hook shows only in transcript mode. Reproduced 2026-08-28 against a fixture
    exiting 3.

    The staleness check ten lines below the caller already separated the two
    states, and says `CONTEXT STALENESS NOT CHECKED` when it could not look. This
    is that fix landing in the second of two adjacent copies. `scope-claims.md`
    obligation 3: a check that could not run reports over, never toward silence.
    """
    script = os.path.join(project_dir, "scripts", "crm-health.py")
    if not os.path.isfile(script):
        # No CRM engine in this workspace. Nothing ran, and nothing was meant to.
        return [], None

    # The checkout's own pinned interpreter, never `sys.executable`: this hook
    # is started by a bare `python3`, which is not the environment crm-health.py
    # was installed into.
    python = os.path.join(project_dir, ".venv", "bin", "python")
    if not os.path.isfile(python):
        return [], f"no .venv/bin/python in {project_dir}, so crm-health.py was not run"

    cache_file = _crm_cache_file(project_dir)
    cache_dir = os.path.dirname(cache_file) if cache_file else None

    # Try cache first
    try:
        if cache_file and os.path.isfile(cache_file):
            with open(cache_file, "r", encoding="utf-8") as f:
                cached = json.loads(f.read())
            cached_at = cached.get("cached_at", 0)
            if (datetime.now().astimezone().timestamp() - cached_at) < _CRM_CACHE_TTL_SECONDS:
                red_lines = cached.get("red_contacts") or []
                # A cache hit is a successful run: only the exit-0 branch below
                # ever writes this file.
                return red_lines, None
    except Exception as e:
        print(f"[session-start] crm-health cache read failed: {e}", file=sys.stderr)

    # Cache miss or stale - run the script
    try:
        result = subprocess.run(
            [python, script],
            capture_output=True, text=True,
            timeout=CRM_HEALTH_TIMEOUT_SECONDS,
            cwd=project_dir
        )
        if result.returncode == 0:
            output = result.stdout
            red_lines = _red_contacts(output)
            # Write cache (best-effort - never block on cache write failure)
            try:
                if cache_file is None:
                    return red_lines, None
                os.makedirs(cache_dir, mode=0o700, exist_ok=True)
                tmp_path = cache_file + ".tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump({
                        "cached_at": datetime.now().astimezone().timestamp(),
                        "red_contacts": red_lines,
                    }, f)
                # chmod the TEMP file, then replace. Doing it the other way
                # round left the cache at the default umask (commonly 0644)
                # between the replace and the chmod, and it holds CRM contact
                # lines. Found by the 2026-08-23 audit. os.replace preserves the
                # source file's mode, so the live file is 0o600 from the instant
                # it exists.
                os.chmod(tmp_path, 0o600)
                os.replace(tmp_path, cache_file)
            except Exception as e:
                print(f"[session-start] crm-health cache write failed: {e}", file=sys.stderr)
            return red_lines, None
        else:
            # `subprocess.run` does not raise on a non-zero exit, so the outer
            # handler never fired and this branch did not exist: a crm-health
            # run that failed produced no alert and no line on any stream. The
            # operator reads an absent CRM alert as "nothing is overdue" while
            # the red debt keeps growing. Every other failure path in this file
            # reports to stderr; this one is now one of them, and the operator
            # also gets an alert, because "the check did not run" is exactly the
            # thing a silent alarm must not hide.
            tail = (result.stderr or result.stdout or "").strip().splitlines()
            detail = tail[-1] if tail else "no output"
            print(f"[session-start] crm-health exited {result.returncode}: {detail}",
                  file=sys.stderr)
            return [], f"crm-health.py exited {result.returncode}: {detail}"
    except Exception as e:
        print(f"[session-start] check_crm-health failed: {e}", file=sys.stderr)
        return [], f"crm-health.py could not be run: {e}"
    return [], None


def check_stale_files(project_dir):
    """Check context files for staleness (>14 days since last verified).

    Two-tier alert:
      - WARNING (>14 days): data getting stale, should refresh soon
      - CRITICAL (>30 days): data unreliable, refresh urgently
    Returns list of (filename, days_old, severity) tuples. Severity is
    WARNING, CRITICAL, or NOT_CHECKED - the last one carrying the reason the
    scan could not run, with days_old 0.
    """
    # context/ lives under the DATA root (HEADING OS split). The branch that
    # read an executive workspace's `corporate/context/` left with the
    # executive model on 2026-09-25. A session
    # launched from the engine clone has no context/ at project_dir, so resolve
    # via get_data_root(), with project_dir as the fallback.
    try:
        sys.path.insert(0, project_dir)
        from scripts.utils.data_homes import homes
        # HOS-240, 2026-10-13: the registry group `context-homes` ALONE, every
        # home the context records live in. Never a list of single names:
        # MEASURED 2026-09-22, a list of names missed two of ten context files
        # and would have reported them fresh by omission.
        #
        # This hook is an INFORMER by the operator's decision of 2026-09-22: it
        # reports a gap on a surface he sees and never returns emptiness in
        # silence.
        context_dirs = [str(p) for p in homes("context-homes")]
    except Exception as e:  # noqa: BLE001 -- best-effort, never blocks start
        # The ONE handler in this file that logged nothing. `get_data_root`
        # raises DataRootError by design when HEADING_OS_DATA names a path
        # that has moved, and this caught it, silently pointed at
        # `<engine>/context` - which does not exist, because context/ is
        # DATA-routed - and returned an empty list. The file's own comment
        # elsewhere names the standard: a freshness alarm that fails toward
        # silence is the worst way for it to fail.
        print(f"[session-start] data-root resolve failed, staleness falls "
              f"back to the engine tree: {e}", file=sys.stderr)
        context_dirs = [os.path.join(project_dir, "context")]
    stale = []
    warn_threshold = datetime.now().astimezone() - timedelta(days=14)
    crit_threshold = datetime.now().astimezone() - timedelta(days=30)

    present = [d for d in context_dirs if os.path.isdir(d)]
    if not present:
        # "nothing to check" and "could not look" are different answers, and the
        # second one was indistinguishable from the first.
        context_dir = "; ".join(context_dirs)
        print(f"[session-start] no context directory at {context_dir}; staleness "
              f"was NOT checked.", file=sys.stderr)
        # A TUPLE, in this function's declared shape. It returned a bare string
        # until 2026-08-27, and the caller unpacks three values per item, so the
        # string was unpacked CHARACTER BY CHARACTER and `main()` died with
        # `ValueError: too many values to unpack (expected 3)`. Every session on
        # a workspace with no context/ directory - which is every fresh public
        # clone, and every engine-only checkout - printed a traceback at
        # SessionStart. No test saw it because they all ran against the
        # operator's own data root, which has a context/ directory.
        return [(f"no context directory at {context_dir}", 0, "NOT_CHECKED")]

    # Every home, not the first one that exists. A `.md` file of the same name
    # in two homes is reported twice rather than once, which is the safe
    # direction for a freshness alarm.
    for context_dir, fname in [(d, f) for d in present for f in os.listdir(d)]:
        if not fname.endswith(".md"):
            continue
        fpath = os.path.join(context_dir, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                for line in f:
                    if "Last verified:" in line or "last verified:" in line.lower():
                        for part in line.split():
                            # `.strip()` removes whitespace only, so a line
                            # ending "Last verified: 2026-06-01." yielded the
                            # token "2026-06-01." and strptime raised, silently
                            # skipping the file. A freshness alarm that fails
                            # toward silence is the worst way for it to fail.
                            # Found by the 2026-08-23 audit; latent, since no
                            # context file carries one today.
                            token = part.strip().strip(".,;:!?()[]{}<>\'\"\u2018\u2019\u201c\u201d")
                            try:
                                d = datetime.strptime(token, "%Y-%m-%d").replace(tzinfo=datetime.now().astimezone().tzinfo)
                                days_old = (datetime.now().astimezone() - d).days
                                if d < crit_threshold:
                                    stale.append((fname, days_old, "CRITICAL"))
                                elif d < warn_threshold:
                                    stale.append((fname, days_old, "WARNING"))
                                break
                            except ValueError:
                                continue
                        break
        except Exception as e:
            print(f"[session-start] check_stale_files error reading {fname}: {e}", file=sys.stderr)
            continue
    return stale


# ============================================================
# Active threads panel
# ============================================================
#
# The `## Active Threads` block in the auto-memory index used to put the running
# set in front of the operator at every session start. It was retired on
# 2026-08-27 because it was a stale COPY: on its last day it listed 3 threads
# against 33 active on disk, and each row quoted a live status and a live date,
# which `.claude/rules/memory-discipline.md` forbids in an always-loaded index.
#
# What the operator lost with it was passive awareness, and that was worth
# keeping. This restores it from the RECORD instead of from a copy: the panel is
# computed from the thread files on every session start, so it cannot go stale,
# and nothing is written anywhere.
#
# Read-only. Console-first: `python scripts/thread.py list` is the same answer
# and stays the primary interface; this is a convenience surface over it.

THREAD_PANEL_DAYS = 14      # a thread untouched for longer is not "running"
THREAD_PANEL_ROWS = 12      # a panel longer than this is a wall, not a summary
THREAD_PANEL_UNREADABLE_NAMED = 3   # name the broken files, then say how many more


def _parse_thread_or_reason(parse_thread_file, path):
    """Parse one thread file. Returns (thread, None) or (None, reason).

    The failure is RETURNED to the caller rather than counted here. A handler
    that absorbed it would make a broken thread file invisible, and an invisible
    broken file is a thread silently missing from the panel.
    """
    try:
        return parse_thread_file(path), None
    except Exception as exc:  # noqa: BLE001 - returned, never dropped
        return None, f"{exc.__class__.__name__}: {exc}"


def _thread_panel_lines(project_dir):
    """Lines for the active-threads panel, or a one-line reason it is absent.

    Returns (lines, note). `note` is non-empty only when the panel could not be
    computed, and it is printed: silence about a check that did not run reads as
    "nothing to report", which is the failure this whole change exists to end.
    """
    root = Path(project_dir).resolve()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from scripts.utils.threads_lib import is_quiet, parse_thread_file, thread_dirs
        from scripts.utils.data_path import data_path
        from scripts.utils.workspace import get_default_tz
    except ImportError as exc:
        return [], f"active-threads panel unavailable ({exc.__class__.__name__}: {exc})"

    try:
        # Every directory a thread sits in, each account's threads among them
        # (HOS-234 part 10): `thread_dirs` walks the registry group
        # `business-threads`. An overlay whose registry cannot answer raises,
        # and the `except` below says so rather than blanking the panel.
        threads_root = data_path('threads')
        dirs = thread_dirs(threads_root)
        today = datetime.now(get_default_tz()).date()
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        return [], f"active-threads panel unavailable ({exc.__class__.__name__}: {exc})"
    if not dirs:
        return [], ""

    active, quiet, unreadable = [], 0, []
    for type_, type_dir in dirs:
        for path in sorted(type_dir.glob("*.md")):
            thread, reason = _parse_thread_or_reason(parse_thread_file, path)
            if thread is None:
                unreadable.append(f"{type_}/{path.name} ({reason})")
                continue
            if thread.status != "active":
                continue
            # A quiet thread must not be surfaced proactively. This panel is the
            # definition of proactive, so it is the first place that rule binds.
            if is_quiet(thread, today):
                quiet += 1
                continue
            try:
                age = (today - date.fromisoformat(thread.last_touched)).days
            except (TypeError, ValueError):
                age = None
            active.append((age, thread))

    if not active and not quiet and not unreadable:
        return [], ""

    # An unparseable date sorts first: it is a defect worth seeing, not a thread
    # worth burying at the bottom of a truncated list.
    active.sort(key=lambda row: (row[0] is not None, row[0] if row[0] is not None else 0))
    recent = [row for row in active if row[0] is None or row[0] <= THREAD_PANEL_DAYS]
    shown = recent[:THREAD_PANEL_ROWS]
    # A row whose `last_touched` failed `date.fromisoformat` has `age is None`
    # and is kept in `recent` on purpose (a broken date is a defect to see, not a
    # thread to bury). But the head then read "Showing the N touched in the last
    # 14 days" over a set that included it, asserting the recency the
    # `except (TypeError, ValueError)` above had just failed to establish.
    # MEASURED 2026-08-31 with one good thread and one carrying
    # `last_touched: "sometime"`: "Showing the 2 touched in the last 14 days."
    # The row itself prints "(no date)", so a careful reader could catch it; the
    # sentence still over-claimed. Counted over `shown`, because `shown` is what
    # the sentence is about. `.claude/rules/scope-claims.md` obligation 2: name
    # what you left out, the way `quiet` and `unreadable` already do.
    undated = sum(1 for row in shown if row[0] is None)

    # Two different reasons a thread is absent, named separately. "20 more
    # active" collapsed them, and a reader cannot tell a thread that went quiet
    # for a month from one the row cap cut off this morning.
    older = len(active) - len(recent)
    head = f"Active threads: {len(active)} active"
    if quiet:
        head += f", {quiet} quiet"
    if unreadable:
        head += f", {len(unreadable)} unreadable"
    head += ". Showing "
    head += (f"{len(shown)} of {len(recent)}" if len(shown) < len(recent)
             else f"the {len(shown)}")
    head += f" touched in the last {THREAD_PANEL_DAYS} days"
    if undated:
        head += f", {undated} with no readable date"
    if older:
        head += f"; {older} older"
    head += "."
    lines = [head]
    for age, thread in shown:
        age_text = "no date" if age is None else ("today" if age == 0 else f"{age}d")
        lines.append(f"- {thread.type}/{thread.id} - {thread.title} ({age_text})")
    # A count says a thread is broken; the name says which one. Without it the
    # operator has to walk the registry by hand to find the file to repair.
    for name in unreadable[:THREAD_PANEL_UNREADABLE_NAMED]:
        lines.append(f"  Unreadable: {name}")
    if len(unreadable) > THREAD_PANEL_UNREADABLE_NAMED:
        lines.append(f"  Unreadable: and {len(unreadable) - THREAD_PANEL_UNREADABLE_NAMED} more")
    lines.append("  Full set: `python scripts/thread.py list`")
    return lines, ""


def check_yard_bootstrap(workspace_root):
    """Warn when a session opens in a YARD whose provisioning did not finish.

    A YARD is a git worktree of this engine. Its `.git` is a FILE rather than a
    directory (MEASURED 2026-09-03), which is how this tells the two apart
    without running anything.

    The bootstrap that provisions a YARD does not start an agent unless it
    reached `status: ok`, so in the ordinary flow this never fires. It exists
    for the session an operator opens BY HAND in a worktree directory: there is
    no bootstrap in that path, and the state it would have refused looks exactly
    like a healthy one.

    What is at stake is not tidiness. In a fresh worktree (MEASURED 2026-09-03)
    the gitignored `.claude/settings.local.json` is ABSENT, so the PreToolUse
    walls, the release gate and the secret scanner among them, are unregistered
    and nothing says so; with `.env` absent too, the data root resolves to no
    data repository.

    An alert, not a block: SessionStart cannot refuse a session, and a hook that
    pretended otherwise would be describing a wall it does not have.
    """
    try:
        if not (workspace_root / ".git").is_file():
            return None                       # the main clone, or not a repo
    except OSError:
        return None

    status_file = workspace_root / ".claude" / ".yard-bootstrap-status"
    hint = ("Re-run it: cd here, then FORCE_BOOTSTRAP=1 "
            "HERDR_PLUGIN_EVENT_JSON='{\"worktree\":{\"path\":\"'\"$PWD\"'\"}}' "
            "bash scripts/herdr/heading-os-yard/yard-bootstrap.sh")

    if not status_file.exists():
        return ("YARD NOT PROVISIONED: this is a worktree and the bootstrap "
                "never ran here. The PreToolUse walls are probably "
                "unregistered and the data root probably resolves to the demo "
                "tree, both of which look healthy. " + hint)

    try:
        state = json.loads(status_file.read_text(encoding="utf-8"))
        status = state.get("status")
        step = state.get("step")
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        # Unreadable is not the same as absent, and neither is "fine". A guard
        # that cannot tell corrupt from missing reports one as the other.
        return (f"YARD BOOTSTRAP STATUS UNREADABLE ({type(exc).__name__}): "
                f"{status_file}. Treat this worktree as unprovisioned. " + hint)

    if status == "ok":
        return None
    # The narrow sentence is deliberate. This function reads a status file; it
    # never asks which PreToolUse walls are registered, so it must not say what
    # this session is running with. `setup-platform.sh --check` is the thing
    # that answers that, and it is named instead of paraphrased.
    return (f"YARD BOOTSTRAP DID NOT COMPLETE: status={status!r} at step "
            f"{step}. Whatever the steps after {step} arm was never armed. "
            f"`bash scripts/setup-platform.sh --check` reports which hook "
            f"registrations are missing here. " + hint)


def check_yard_marker(workspace_root):
    """Warn when a session runs in a YARD without `HEADING_OS_YARD` in its env.

    The bootstrap starts the agent as `herdr pane run <pane> "export
    HEADING_OS_YARD=1; <agent>; ..."` (no `exec`, so the pane keeps a shell when
    the agent exits), and that export is the whole point of the line: it is
    in the AGENT PROCESS, inherited by its shells, their children and
    the git hooks those children run, and it is what the yard-side guards read.

    MEASURED 2026-09-06 in `.yard/.heading-os/yard-yard-deletion-guard`: an
    agent started BY HAND, from an older instruction that carried no marker, won
    the race against the bootstrap's own line by 0.8 s. The bootstrap's command
    then arrived at an agent that was already alive and was consumed as a
    PROMPT. `/proc/<pid>/environ` held no `HEADING_OS_YARD`, the bootstrap wrote
    an honest `status: ok` at step 11 because it had done its part, and nothing
    anywhere reported that the session was running with the marker absent.

    The "am I in a yard" signal is `.git` being a FILE, the same property
    `check_yard_bootstrap` above uses, and deliberately NOT the variable this
    function is checking for: a check that read the marker to decide whether the
    marker matters would prove itself and fire never.

    An alert, not a block. SessionStart cannot refuse a session, and the repair
    is one command the operator runs.
    """
    try:
        if not (workspace_root / ".git").is_file():
            return None                       # the main clone, or not a repo
    except OSError:
        return None

    if os.environ.get("HEADING_OS_YARD"):
        return None

    return ("YARD MARKER MISSING: this is a worktree and `HEADING_OS_YARD` is "
            "absent from this session's environment, so every guard that reads "
            "it is inert here. It is exported into the agent PROCESS, so it "
            "cannot be repaired from inside this session and no file carries "
            "it. Close this session and start it as: cd here, then "
            "`HEADING_OS_YARD=1 claude --dangerously-skip-permissions`.")


def check_yards_without_a_session(workspace_root):
    """Name every worktree of this repository with no live Claude session in it.

    The sibling above asks about THIS checkout. This one asks about the others,
    and it is a separate question with a separate answer: a worktree can be
    provisioned to `status: ok` and still have no agent in it.

    MEASURED 2026-09-06: `herdr worktree create` opens a SHELL and has no flag
    that starts an agent, so the agent arrives only through the bootstrap
    plugin's last step, which is skipped in silence when no pane resolves,
    skipped by design under `HEADING_OS_AUTOSTART=0`, short-circuited on a
    re-open of a healthy yard, and reports success on `herdr pane run` exiting
    zero -- which means DISPATCHED, not booted. The operator hit the result
    twice that day: a yard that looks alive, has nothing to show, and gets a
    neighbouring yard's session rendered into its slot.

    HELM ONLY, and that is the point rather than an optimisation. This alert is
    about the fleet, HELM is the one session the operator sits at himself, and
    firing it in every yard would print the same list N times. `.git` being a
    DIRECTORY is how the main clone is told apart, the same predicate the
    sibling above uses in the other direction.

    An alert, never a block, and never an action: starting an agent is a change
    to what runs on this machine, which a hook does not get to make on its own.

    THE SIGNAL CHANGED ON 2026-09-06, and the reason is in
    `scripts/utils/yard_sessions.py`: a transcript answers about the past, so a
    correctly provisioned yard whose agent had not been spoken to was named,
    and a yard whose agent had exited was not. It is now a live process with
    `comm == "claude"` whose cwd IS the checkout, with the transcript kept only
    for an unreadable `/proc` and reported as the weaker signal when it is used.

    Against the hook's time budget: MEASURED 2026-09-06, 3.7 ms per sweep over
    one registered worktree for the git side, plus 0.8 ms per yard for the
    `/proc` walk over 76 processes. It scales with the number of yards and the
    number of processes, never with the size of a tree.
    """
    try:
        if not (workspace_root / ".git").is_dir():
            return None                       # a YARD, or not a checkout at all
    except OSError:
        return None

    def _import():
        from scripts.utils import yard_sessions
        return yard_sessions

    module = _load_engine_module(
        "scripts/utils/yard_sessions.py", "yard_sessions", _import,
        "no worktree is checked for a transcript")
    if module is None:
        return ("YARD SESSION SWEEP UNAVAILABLE: scripts/utils/yard_sessions.py "
                "could not be imported, so nothing established which worktrees "
                "hold a transcript. Treat the sidebar as unverified.")

    try:
        # No `exclude=` here on purpose. It exists for a caller running INSIDE a
        # yard, whose own transcript may not be on disk yet at SessionStart;
        # this one runs in HELM, which `worktree_roots` never returns, so an
        # exclusion of `workspace_root` could not change any answer.
        report = module.yards_without_a_session(workspace_root)
    except Exception as exc:  # noqa: BLE001 - an alert surface never crashes
        return (f"YARD SESSION SWEEP FAILED ({type(exc).__name__}: {exc}). No "
                "worktree was checked for a transcript.")

    # `unknown` is no longer exclusive with a result. Since 2026-09-06 the sweep
    # falls back to the transcript when `/proc` cannot be read, and says so
    # there rather than returning nothing, so the caveat is PREPENDED to the
    # names instead of replacing them. Returning early on it would throw away
    # the answer whenever the weaker signal was the one available.
    caveat = f" (CAVEAT: {report.unknown})" if report.unknown else ""
    if report.unknown and not report.checked:
        return f"WORKTREES NOT CHECKED FOR A LIVE AGENT: {report.unknown}."
    if not report.silent:
        return None

    names = [p.name for p in report.silent]
    shown = ", ".join(names[:8])
    if len(names) > 8:
        shown += f", and {len(names) - 8} more"
    return (f"{len(report.silent)} of {len(report.checked)} worktree(s) have no "
            f"Claude session standing in them: {shown}{caveat}. "
            f"`herdr worktree create` opens a shell, not an agent, and a yard "
            f"without one shows a NEIGHBOUR's work in the sidebar. Start one: "
            f"cd into the yard, then "
            f"`HEADING_OS_YARD=1 claude --dangerously-skip-permissions`.")


# ============================================================
# The private operational layer
# ============================================================
#
# The engine's `CLAUDE.md` carries no import of the operational file (it did
# until 2026-09-20). The layer arrives through the USER-level `~/.claude/CLAUDE.md`,
# which imports it by absolute path and so reaches the main clone and every
# yard alike; a checkout-relative import resolved to nothing in a yard, two
# levels below the main clone (MEASURED 2026-09-08). Whether the harness
# follows an import out of a project is settled by a per-project approval it
# records in `~/.claude.json` (MEASURED 2026-09-09), so this hook never answers
# that question: it asks only whether a root a session loads names the layer,
# and stays silent where one does.
#
# Cost: one 8 KB read, far inside the 15 s budget above.
#
# THE LAYER IS A CHAIN, NOT A FILE, and until 2026-09-20 this appended one file.
# The harness follows an `@` import inside an imported file, four hops below the
# root, resolving each relative path against the directory of the file that
# CONTAINS it. This hook did a single `read_text()`, so an `@CLAUDE.private.md`
# written inside `CLAUDE.operational.md` arrived in a yard as that literal text
# and the file behind it did not arrive at all. In the main clone the harness
# follows the same import and the rules DO arrive, so the two shapes disagree in
# exactly the place nobody is watching: a yard is where nearly all engine work
# happens, and the yard, daemon, commit and publication prohibitions are what
# that second hop is about to carry.
#
# The traversal itself is `scripts/utils/claude_md_chain.py`, shared with
# `scripts/utils/memory_health.py`, which asks whether a policy file is
# reachable from `CLAUDE.md` before it will accept a shrunken memory index. Two
# copies of one traversal are two answers to one question.
#
# ------------------------------------------------------------------
# 2026-09-20: THE ROUTE ABOVE WAS NEVER BIG ENOUGH, AND NOTHING SAID SO.
# ------------------------------------------------------------------
#
# Hook stdout is capped. MEASURED 2026-09-20 out of the shipped bundle, Claude
# Code 2.1.277: `DJr=1e4` and `if(e.length<=s)return e`, so at most 10,000
# UTF-16 code units are injected whole and anything longer is written to a file
# with only a ~2000-character preview reaching the session and NOTHING printed
# in the terminal. The full constants, the persisting function and the
# behavioural confirmation are in `claude_md_chain`.
#
# The chain this hook appends for a yard is about 35,000 bytes. So from the day
# the second hop was added, roughly 2,000 characters of it arrived and the rest
# was persisted in silence, which is the exact failure the second hop was added
# to end, one layer up. A hook cannot carry the conduct rules. It never could.
#
# What carries them is the USER-level `~/.claude/CLAUDE.md`, which is not inside
# any project, is loaded at depth 0 like a project root, has no cap, and MEASURED
# the same day delivers byte-identical content to the main clone and to a
# worktree. That file is the operator's and HELM's; this hook never writes it.
#
# So this hook's job changed shape. It is no longer the delivery route. It is
# the thing that NOTICES the delivery route is missing, and says so in bytes
# small enough to arrive. Where a route exists it stays silent; where none does
# and the payload would fit, it still injects; where none does and the payload
# would not fit, it refuses and prints the repair instead of 35,000 units of
# rules nobody will see.
#
# The engine's own `CLAUDE.md` stopped importing the operational file on the
# same day, and that is why this hook must ask more than one root. Two roots
# walking the same chain do not share a dedup set, so the main clone loaded the
# whole policy layer TWICE, about 35,000 units each, and a reader finding two
# copies of a rule cannot tell they are the same copy.

OPERATIONAL_LAYER_FILENAME = "CLAUDE.operational.md"

# STILL A LITERAL, joined onto the data root in `check_operational_layer` below,
# and that is a recorded finding rather than an oversight.
#
# HOS-5 phase 3 batch 1 routed it through the front door as
# `data_path("operational-layer")` on 2026-09-20 and reverted it the same day.
# The address resolved correctly on the operator's tree. What a `names:` entry
# cannot do is answer on a data root that has no `config/data-paths.yaml`, and
# this hook is positioned exactly where that matters.
#
# MEASURED 2026-09-20 in the yard hos5-phase3-hooks, on a bare data root:
#
#     runtime:<service>/<kind>   ANSWERS  (`services:` has a shipped template)
#     record:<family>/<leaf>     REFUSES  (no HEADING OS 2 manifest)
#     any `names:` entry         REFUSES  (no registry file at all)
#
# So the conversion made "no registry" indistinguishable from "no layer", and
# the injection-size checks went red in six places, each building an overlay
# that holds this file and no `config/`: the hook returned state 1, silent,
# where it had injected or refused aloud. A layer that silently did not arrive
# is the one failure this function exists to end, so the silent state may not
# be widened to cover a missing registry. Those six were parked in the
# 2026-09-21 test-suite split, so the same conversion would now go green.
#
# The precondition for moving this line is a registry present in EVERY overlay,
# or a shipped default for `names:` of the kind `runtime_dir.load_services`
# already gives `services:`. Both are changes to `scripts/utils/data_path.py`
# and an operator decision, not a hook edit.
#
# Meanwhile the literal has one property worth naming, because it is why the
# revert costs little: `config/routing-map.yaml` carries an explicit `private`
# rule for this same spelling, so the file's CLASSIFICATION is already derived
# from a single source even while its address is not.

# The first line of the marker printed when the layer cannot be delivered at
# all. Named rather than spelled at the use site because `main()` has to put
# this one ahead of every other section it prints, and a marker matched by
# prefix is a contract between two functions.
UNDELIVERABLE_MARKER_PREFIX = "OPERATIONAL LAYER NOT DELIVERED"


def _load_chain_module():
    """`scripts.utils.claude_md_chain`, or None on a clone without it.

    OPTIONAL for the same reason every other loader in this file is: a hook that
    cannot import a helper must still deliver what it can. Without it the caller
    falls back to the single-file read this hook did before 2026-09-20 and SAYS
    SO in the marker, because a layer that arrived incomplete and a layer that
    arrived whole must not print the same line.
    """
    def _import():
        from scripts.utils import claude_md_chain
        return claude_md_chain

    return _load_engine_module(
        "scripts/utils/claude_md_chain.py", "claude_md_chain", _import,
        "only the operational file itself is injected, not the files it imports")


def _operational_import_target(workspace_root, chain_module=None):
    """Resolve the path `CLAUDE.md`'s `@` import of the operational layer names.

    Resolved against the directory of the file that CONTAINS the import, which
    is how the harness reads it, and derived from the checkout handed in rather
    than from a constant or from the common git directory: a worktree's
    `CLAUDE.md` is its own file and a guard that read the main clone's would
    answer for the wrong tree.

    None when this checkout's `CLAUDE.md` names no such import, which is also
    what an unreadable `CLAUDE.md` returns -- both mean "nothing here can carry
    the layer", and the caller injects.

    The spellings come from the shared parser when it is importable, so what
    counts as an import here is what counts as one in the traversal below: an
    `@` inside a fenced block or an inline span is not one. The line scan is the
    fallback for a clone without the module, and it is deliberately the looser
    of the two - it can only ever find MORE imports, which sends this function
    toward "the import names a path" and the caller toward silence, and a layer
    injected twice is cheaper than one injected on top of a live import.
    """
    claude_md = workspace_root / "CLAUDE.md"
    try:
        text = claude_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if chain_module is not None:
        spellings = chain_module.import_spellings(text)
    else:
        spellings = [line.strip()[1:].strip() for line in text.splitlines()
                     if line.strip().startswith("@")]
    for named in spellings:
        if not named.endswith(OPERATIONAL_LAYER_FILENAME):
            continue
        if chain_module is not None:
            return chain_module.resolve_import(named, claude_md.parent)
        try:
            return (claude_md.parent / named).resolve()
        except OSError:
            return None
    return None


def _delivering_roots(workspace_root, source, chain_module):
    """Every root a session already loads whose `@` chain carries `source`.

    Returns a list of `(label, root path)`, in the order the roots are asked.
    Empty means nothing delivers the operational layer and this hook is the only
    candidate route left.

    TWO ROOTS, AND ASKING BOTH IS THE FIX FOR A DUPLICATE, NOT A BELT AND
    BRACES. MEASURED 2026-09-20 in the main clone, by asking a live session to
    count the section heading `## Authority and approval` in its loaded
    instructions: the answer was 2. The project root imported the operational
    file by a parent-relative path that resolves in the main clone, and the
    user-level root imported the same file by an absolute one so that a worktree
    would get it too. Inside ONE walk the harness dedups on the resolved path;
    two separate walks do not share that set, so the main clone paid ~35,000
    UTF-16 units twice and a reader finding two copies of a rule has no way to
    know they are the same copy. The engine's own `CLAUDE.md` gave up its import
    that day. This function still asks it, because another operator's clone may
    wire it and a hook that stopped asking would inject on top of their live
    import.

    With no `chain_module` this degrades to the pre-2026-09-20 question, which
    is whether the path the project import NAMES exists. That is looser in the
    safe direction for the project root and blind for the user-level one, and
    the caller says so in its marker.

    The decision itself is `claude_md_chain.delivering_roots`, which carries why
    the two roots are asked different questions. It lives there rather than here
    because `scripts/check-operational-layer-armed.py` asks the same question to
    decide whether a clone is armed, and two copies of one answer drift.
    """
    if chain_module is not None:
        return chain_module.delivering_roots(
            Path(workspace_root) / "CLAUDE.md", source)

    roots = []
    target = _operational_import_target(workspace_root, None)
    try:
        if target is not None and target.is_file():
            roots.append(("CLAUDE.md", Path(workspace_root) / "CLAUDE.md"))
    except OSError:
        pass
    return roots


def _chain_marker_tail(chain):
    """What the chain loaded and what it did not, as one sentence each.

    Separate from the marker itself so the three failure markers above keep
    their own wording; this runs only where bytes are actually being appended.
    """
    names = ", ".join(f.path.name for f in chain.files)
    tail = f" CHAIN: {names}."
    if chain.problems:
        tail += (" NOT LOADED: "
                 + "; ".join(p.describe() for p in chain.problems) + ".")
    return tail


def check_operational_layer(workspace_root, printed_utf16_units=0):
    """Return `(urgent, marker, body)` for the private operational layer.

    `urgent` is a single line that `main()` prints AHEAD of everything else it
    prints, or None. It exists because of the cap: past 10,000 UTF-16 units only
    the first ~2,000 characters of this hook's whole output reach the session,
    so a line that has to be read has to be first. Everything else keeps its
    old place at the end.

    `printed_utf16_units` is what the rest of this hook is about to print. The
    cap is per INVOCATION, so the alert block, the CRM line and the thread panel
    spend the same budget as this does, and a check that measured only its own
    section would pass while the pair of them overflowed.

    Four states, and which one this is has to be readable from the output rather
    than from this source:

    1. **No overlay content.** A public clone has no data repository, and
       `get_data_root()` lands on the `examples` sentinel, which holds no
       operational file. Nothing to inject and nothing wrong: silent.
    2. **A root a session loads already carries it.** Either the project's own
       `CLAUDE.md` or the user-level `~/.claude/CLAUDE.md`. Appending would pay
       the same bytes twice in the resident context of every session, which is
       the duplicate MEASURED in the main clone on 2026-09-20: silent.
       Deliberately NOT "and the root is inside this checkout": whether the
       harness follows a path out of a project is settled by a per-project
       approval it records in `~/.claude.json`, which this hook cannot read the
       meaning of and must not second-guess. See the block above.
    3. **No root carries it, and the whole output still fits under the cap.**
       Inject the bytes OF THE WHOLE CHAIN rooted at the operational file, in
       the harness's own order, with anything that could not be loaded named in
       the marker. This is the pre-2026-09-20 behaviour, kept because it is
       correct whenever it can actually work.
    4. **No root carries it and the output does NOT fit.** INJECT NOTHING and
       say so in bytes small enough to arrive. Printing the body here would
       push the whole output over the threshold, at which point the terminal
       shows nothing, the session gets a preview, and the operator is told
       neither. The refusal names every file that did not arrive by absolute
       path, so the session can read them itself, and names the one-line repair:
       an `@` import of the operational file in the user-level root. This hook
       never writes that file. It is outside both repositories and HELM owns it.

    The data root comes from `get_data_root()`, the workspace's one resolver for
    it, so an operator who moves the overlay with `HEADING_OS_DATA` moves this
    too. A failure to resolve or to read is reported as its own marker with no
    body: an operational layer that silently did not arrive is the defect this
    function exists to end, so it never fails quiet.
    """
    def _import():
        from scripts.utils import paths
        return paths

    module = _load_engine_module(
        "scripts/utils/paths.py", "paths", _import,
        "the private operational layer is not injected")
    if module is None:
        return (None,
                "OPERATIONAL LAYER NOT INJECTED: scripts/utils/paths.py could "
                "not be imported, so the data root was never resolved. The "
                "engine/data topology and the outputs rule are absent from this "
                "context.", None)

    try:
        source = Path(module.get_data_root()) / OPERATIONAL_LAYER_FILENAME
    except Exception as exc:  # noqa: BLE001 - an alert surface never crashes
        return (None,
                f"OPERATIONAL LAYER NOT INJECTED: the data root did not resolve "
                f"({type(exc).__name__}: {exc}).", None)

    try:
        if not source.is_file():
            return (None, None, None)                 # state 1
    except OSError:
        return (None, None, None)

    chain_module = _load_chain_module()
    if _delivering_roots(workspace_root, source, chain_module):
        return (None, None, None)                     # state 2

    target = _operational_import_target(workspace_root, chain_module)
    where = ("names a path that does not exist here" if target is not None
             else "carries no import of it")

    if chain_module is None:
        # Degraded, and it says which half is missing. The operational file's
        # own bytes still arrive; anything it imports does not, the user-level
        # root was never asked, and the cap was never measured. Three losses,
        # named, rather than one line that reads like success.
        try:
            body = source.read_text(encoding="utf-8")
        except OSError as exc:
            return (None,
                    f"OPERATIONAL LAYER NOT INJECTED: {source} is present but "
                    f"unreadable ({exc}).", None)
        return (None,
                f"OPERATIONAL LAYER APPENDED BELOW ({len(body.encode('utf-8'))} "
                f"bytes from 1 file at {source}): CLAUDE.md {where}. CHAIN NOT "
                f"FOLLOWED: scripts/utils/claude_md_chain.py could not be "
                f"imported, so any file this one imports is ABSENT, the "
                f"user-level root was not asked, and the hook output cap was "
                f"not checked.", body)

    # ONE LESS LEVEL THAN THE HARNESS ALLOWS, and the arithmetic is the point.
    # In the main clone the harness loads a root at depth 0 and the operational
    # file it imports at depth 1, so the deepest file it will ever reach below
    # the operational file is the harness's depth 4 -- three hops. Rooting this
    # chain at the operational file and spending the full budget would inject a
    # file a real session never sees, which is this hook making HELM and a yard
    # disagree in the other direction.
    chain = chain_module.resolve_chain(
        source, max_depth=max(1, chain_module.HARNESS_MAX_DEPTH - 1))
    if not chain.root_readable:
        reason = (chain.problems[0].reason if chain.problems
                  else "no reason recorded")
        return (None,
                f"OPERATIONAL LAYER NOT INJECTED: {source} is present but "
                f"unreadable ({reason}).", None)

    body = chain.text
    marker = (f"OPERATIONAL LAYER APPENDED BELOW "
              f"({len(body.encode('utf-8'))} bytes from {len(chain.files)} "
              f"file(s) rooted at {source}): CLAUDE.md {where}."
              + _chain_marker_tail(chain))

    limit = chain_module.HOOK_STDOUT_UTF16_LIMIT
    units = chain_module.utf16_units(marker) + chain_module.utf16_units(body)
    total = printed_utf16_units + units
    if total <= limit:
        return (None, marker, body)                   # state 3

    # State 4. Short, first, and every path absolute, because this is the only
    # part of it anyone will read.
    user_root = chain_module.user_instruction_root()
    missing = ", ".join(str(f.path) for f in chain.files)
    return (f"{UNDELIVERABLE_MARKER_PREFIX}: no CLAUDE.md a session loads "
            f"imports {source}, and this hook cannot carry it: the layer is "
            f"{units} UTF-16 units and hook stdout is capped at {limit} "
            f"(MEASURED 2026-09-20, `DJr` in Claude Code 2.1.277), past which "
            f"only the first {chain_module.HOOK_PERSISTED_PREVIEW_CHARS} "
            f"characters arrive and the rest is persisted to a file in silence."
            f" NOT LOADED: {missing}. Read those files now, or add the line "
            f"`@{source}` to {user_root}, which has no cap and reaches this "
            f"worktree and the main clone alike. "
            f"({printed_utf16_units} of the budget is already spent by this "
            f"hook's other sections.)",
            None, None)


def main():
    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[session-start] failed to parse input: {e}", file=sys.stderr)
        input_data = {}

    # A payload that is valid JSON but not an object still has `.get` called on
    # it. `[]`, `"x"` and `3` all parse, then raise an uncaught AttributeError.
    # Measured 2026-08-23 with `echo '[]' | python <hook>`: exit 1, traceback.
    # `.claude/hooks/checkpoint-inject.py` fixed this shape on 2026-08-20 and
    # these were missed. Degrade to the empty dict, which every path below
    # already handles, rather than dropping the hook's whole job.
    if not isinstance(input_data, dict):
        print(f"[session-start] payload was {type(input_data).__name__}, not an "
              "object; continuing with defaults", file=sys.stderr)
        input_data = {}

    # A `cwd` key holding null, a number or a list crashed the hook above all
    # its alerts (MEASURED 2026-09-01); the same guard as prompt-guard.py and
    # post-write-sanitize.py.
    project_dir = input_data.get("cwd")
    if not isinstance(project_dir, str) or not project_dir:
        project_dir = os.getcwd()
    workspace_root = Path(project_dir)
    alerts = []

    yard_alert = check_yard_bootstrap(workspace_root)
    if yard_alert:
        alerts.append(yard_alert)

    marker_alert = check_yard_marker(workspace_root)
    if marker_alert:
        alerts.append(marker_alert)

    fleet_alert = check_yards_without_a_session(workspace_root)
    if fleet_alert:
        alerts.append(fleet_alert)

    # Check CRM health. Two separate states: the check failed, and the check ran
    # and found overdue contacts. A count is only meaningful in the second.
    red_contacts, crm_failure = check_crm_health(project_dir)
    if crm_failure:
        alerts.append(f"CRM HEALTH CHECK NOT RUN: {crm_failure} "
                      f"-- overdue contacts are UNKNOWN, not zero")
    if red_contacts:
        alerts.append(f"CRM ALERT: {len(red_contacts)} contact(s) need attention today")

    # Check stale context files (two-tier: >14d warning, >30d critical)
    stale = check_stale_files(project_dir)
    if stale:
        critical = [f"{f} ({d}d)" for f, d, s in stale if s == "CRITICAL"]
        warning = [f"{f} ({d}d)" for f, d, s in stale if s == "WARNING"]
        not_checked = [f for f, _d, s in stale if s == "NOT_CHECKED"]
        if not_checked:
            # "could not look" is not "nothing is stale", and the alert has to
            # say which one it is.
            alerts.append("CONTEXT STALENESS NOT CHECKED: "
                          + "; ".join(not_checked))
        if critical:
            alerts.append(f"STALE DATA (CRITICAL): {', '.join(critical)} -- data unreliable, update urgently")
        if warning:
            alerts.append(f"STALE DATA (WARNING): {', '.join(warning)} -- approaching staleness, refresh soon")

    # Every stdout line this hook produces from here down, collected rather than
    # printed. The setup banner that once wrote straight to stdout above this
    # point, uncounted, left on 2026-09-25.
    out = []

    if alerts:
        # PLAIN TEXT on stdout, which SessionStart adds as context. An earlier
        # version wrote JSON under a key the SessionStart schema does not define.
        out.append("Session alerts:")
        out.extend(f"- {alert}" for alert in alerts)

    panel, panel_note = _thread_panel_lines(project_dir)
    if panel_note:
        print(f"[session-start] {panel_note}", file=sys.stderr)
    if panel:
        if alerts:
            out.append("")
        out.extend(panel)

    # BUFFERED RATHER THAN PRINTED, and that is the whole reason this loop
    # changed shape on 2026-09-20. The harness caps one hook invocation's stdout
    # at 10,000 UTF-16 units and persists the overflow to a file behind a ~2,000
    # character preview, saying nothing anywhere. Every section above spends
    # that budget, so the operational-layer check has to be told what is already
    # spent before it decides whether it can print at all.
    preamble = "\n".join(out)
    spent = 0
    _chain = _load_chain_module()
    if _chain is not None:
        spent = _chain.utf16_units(preamble)

    # Last, and on stdout with everything else. The marker goes first so the
    # one line that says what happened is not buried under the bytes it
    # describes, and it is printed even when the body is absent, because a
    # layer that did not arrive is exactly what nothing reported before.
    op_urgent, op_marker, op_body = check_operational_layer(
        workspace_root, printed_utf16_units=spent)

    # AHEAD OF EVERYTHING, and only ever one short line. Nothing else this hook
    # prints has to be read to keep the session correct; this does, and the
    # preview is cut from the FRONT, so first is the only place it survives.
    if op_urgent:
        print(op_urgent)
        if preamble:
            print()
    if preamble:
        print(preamble)
    if op_marker or op_body:
        if preamble or op_urgent:
            print()
        if op_marker:
            print(op_marker)
        if op_body:
            print(op_body)

    sys.exit(0)


if __name__ == "__main__":
    main()
