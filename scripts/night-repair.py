#!/usr/bin/env python3
"""The night batch: approve open findings by day, repair them while nobody works.

Usage:
    python scripts/night-repair.py --status
    python scripts/night-repair.py --approve            # everything open
    python scripts/night-repair.py --approve --max-minutes 180
    python scripts/night-repair.py --run                # started by hand

What runs at night, and what cannot
-----------------------------------
`--run` starts a headless Claude Code session over an approved batch. Three
properties bound it, held by the code below and by no test:

1. **It cannot commit and it cannot push.** The prompt it passes carries no word
   from the release gate's authorising lists, so `check_release_gate` in
   `.claude/hooks/_dispatch.py` refuses any commit, tag, push or publish the
   session attempts. That is deliberate, and it is why the night is safe: an
   authorising word placed in a machine-written prompt would let a program grant
   itself the operator's permission, which is the exact defect the gate was
   built for after a release went out on an approval nobody had given.
2. **It cannot run twice.** The batch is consumed before the session starts, so
   a crash, a reboot or a second timer fire finds nothing to do.
3. **It cannot approve itself.** `--approve` writes the batch, and only the
   operator runs it. `--run` refuses when no approved batch is waiting.

A pass that ends early is continued, twice at most
--------------------------------------------------
`claude -p` exits 0 whenever the session ends a turn, and a turn can end with a
report while items are still owed. Anthropic's Opus 5.5 guidance on unattended
runs, adopted on the operator's decision of 2026-09-27, treats that as a report
rather than as proof the work is done. So the session gets the guidance's
standing paragraph about early stops as an appended system prompt, writes one
line per item to a report file named in the prompt, and after it exits the
report is compared with the batch. Items with no line and no stated blocker are
named back to the same session through `--resume`, at most
`MAX_CONTINUATIONS` times, so a pass that is genuinely stuck still ends. What
remains open is printed at the end and makes the exit code 1.

The morning is the acceptance
-----------------------------
The session leaves a working tree the operator reads. It does not mark anything
`fixed` in the rotation ledger either: a repair is recorded as fixed once its
evidence exists (a reproduction run that no longer reproduces and a Fix-Note
line, or for failure A or B a test that was red) and the operator accepts it. An agent that both repairs and certifies its own repair is marking its own
homework, which is the shape this whole standard exists to remove.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.colors import BOLD, CYAN, GRAY, GREEN, RED, RESET, YELLOW  # noqa: E402
from scripts.utils.checkpoint_autonomy import UNATTENDED_STANDING_INSTRUCTION  # noqa: E402
from scripts.utils.checkpoint_paths import state_root  # noqa: E402
from scripts.utils.workspace import get_default_tz, get_workspace_root  # noqa: E402

ROOT = get_workspace_root()
BATCH_PATH = state_root(ROOT) / "night-batch.json"
LOG_DIR = state_root(ROOT) / "night-repair"
HOLD_PATH = ROOT / "config" / "automation-hold.json"

# The session's own budget. A night is long, but not unbounded, and a run that
# never returns is a run nobody notices failed.
DEFAULT_TIMEOUT_S = 6 * 3600

# Automatic continuations after the first session exits with items still open.
# The guidance says "two or three"; two, because each one is a full session
# resumed against the same budget above.
MAX_CONTINUATIONS = 2

# One report line per item: "- item 3: DONE ...", "- item 3: NOT REPRODUCED ...",
# "- item 3: BLOCKED: <what blocks it>". Numbered, because one file can carry
# several findings, so a path does not identify an item.
# The status word is required: the prompt lists the batch as "- item N: <path>",
# and a copy of that list in the report must not read as N items reported.
_REPORT_LINE = re.compile(
    r"^\s*[-*]?\s*item\s+(\d+)\s*:\s*(DONE|NOT REPRODUCED|BLOCKED)\b(.*)$",
    re.IGNORECASE,
)


def _now() -> str:
    return datetime.now(get_default_tz()).isoformat(timespec="seconds")


def hold_reason(path: Path, today: str) -> str | None:
    """Why nothing may run today, or None when the freeze has lapsed.

    A dated freeze the operator sets by hand. It exists because a hold an
    assistant merely REMEMBERS is not a hold: the release that went out on
    2026-08-31 was a permission remembered rather than read, and this is the
    same shape pointed the other way. A date on disk survives a compaction, a
    new session, and a different assistant.

    An unreadable or malformed file HOLDS rather than lapses. Absent is the only
    state that means "no freeze", because absent is the only one that carries
    the operator's intent; a corrupt file carries no intent at all and the safe
    reading of no intent is to do nothing.
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"the hold file is unreadable ({exc}); refusing rather than guessing"
    if not isinstance(data, dict):
        return "the hold file is malformed; refusing rather than guessing"
    until = data.get("hold_until")
    if not isinstance(until, str) or not until:
        return "the hold file names no date; refusing rather than guessing"
    if today >= until:
        return None
    return (f"held until {until} ({data.get('reason') or 'no reason recorded'}); "
            f"today is {today}")


def _rotation():
    """The rotation module, imported by path (its filename is kebab-case)."""
    import importlib.util

    path = ROOT / "scripts" / "audit-rotation.py"
    spec = importlib.util.spec_from_file_location("audit_rotation_for_night", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ============================================================
# The batch
# ============================================================

def build_batch(findings, max_minutes: int | None) -> dict:
    """The approved work, in severity order, cut at the time budget.

    The budget STOPS the batch, it does not pack it. Skipping an item that does
    not fit and taking a smaller one behind it uses the night better and makes
    the batch stop being the top of the list, which is the property the operator
    reads it for: what got approved is the most severe work, in order, cut where
    the time ran out.

    One carve-out. A finding whose own estimate exceeds the whole budget is
    still taken when it is first, because otherwise the largest defect can never
    be scheduled and quietly outlives every night.
    """
    items = []
    spent = 0
    for rel, finding in findings:
        minutes = int(finding.get("estimate_minutes") or 0)
        if max_minutes is not None and items and spent + minutes > max_minutes:
            break
        items.append({"path": rel, **finding})
        spent += minutes
    return {"approved_at": _now(), "consumed_at": None,
            "estimated_minutes": spent, "items": items}


def load_batch(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # Never swallow: an unreadable batch is a fault. Reading it as "no batch"
        # would turn a corrupt file into a silent no-op night after night.
        print(f"{RED}batch unreadable: {exc}{RESET}", file=sys.stderr)
        raise SystemExit(2) from exc
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        print(f"{RED}batch malformed: expected an object with an items list"
              f"{RESET}", file=sys.stderr)
        raise SystemExit(2)
    return data


def save_batch(path: Path, batch: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(batch, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def is_pending(batch: dict | None) -> bool:
    """An approved batch nobody has started yet."""
    return bool(batch) and batch.get("consumed_at") is None and bool(batch["items"])


# ============================================================
# The prompt
# ============================================================

def build_prompt(batch: dict, report: Path) -> str:
    """The instruction the night session receives.

    Carries NO authorising word from the release gate's lists, and neither
    does `build_continuation`; no test checks that, so check the words below
    against `_PUSH_WORDS` and `_COMMIT_WORDS` in `.claude/hooks/_dispatch.py`
    before changing them.
    """
    lines = [
        "Unattended repair pass. The operator approved this batch during the "
        "day and is asleep. Work through every item in it; the pass is over "
        "when each item has its line in the report named below.",
        "",
        "For each item: open the current file first and reproduce the defect "
        "with a run, the command that makes it happen. If it does not "
        "reproduce, say so and leave the file alone. If it does, apply the fix "
        "and run the same command again to show the defect gone.",
        "",
        "Write a test only when the defect is failure A or failure B of Phase 5 "
        "in .claude/rules/development-standards.md. For such a test: write it "
        "before the fix, watch it fail, apply the fix, watch it pass, then "
        "mutation-verify with scripts/utils/mutation_harness.py and state the "
        "fraction caught. Every other fix gets NO test and one line "
        "`Fix-Note: <what broke> | <what fixed it> | <why no test>`.",
        "",
        "Follow .claude/rules/development-standards.md, section 'The Evidence "
        "Standard' and Phase 5 of 'Debugging Discipline'; where they differ on "
        "tests, Phase 5 wins.",
        "",
        "Leave the working tree dirty. Do not stage anything, do not create a "
        "branch, do not touch the rotation ledger. The operator reads the diff "
        "in the morning and decides.",
        "",
        f"The batch ({len(batch['items'])} item(s), "
        f"{batch['estimated_minutes']} minutes estimated):",
    ]
    for number, item in enumerate(batch["items"], 1):
        lines.append(f"- item {number}: {item['path']}: {item['summary']} "
                     f"[{item['severity']}, ~{item['estimate_minutes']}m]")
    lines += [
        "",
        f"Write what you did to {report}, one line per item, starting "
        "`- item N:` and then DONE, NOT REPRODUCED, or BLOCKED: followed by "
        "what blocks it. After that word: the reproduction command, the "
        "Fix-Note line (or, for failure A or B, the test file and the mutation "
        "fraction), and for anything you left alone, the reason.",
    ]
    return "\n".join(lines)


def report_status(batch: dict, report: Path) -> tuple[list[int], list[int]]:
    """(open, blocked) item numbers, read from the report the session wrote.

    Open means no line for the item carrying one of the three status words.
    An absent or unreadable report
    leaves every item open, which is what the morning should hear.
    """
    lines: dict[int, str] = {}
    try:
        text = report.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    for raw in text.split("\n"):
        match = _REPORT_LINE.match(raw)
        if match:
            lines[int(match.group(1))] = match.group(2).upper()
    total = len(batch["items"])
    blocked = [n for n in range(1, total + 1)
               if lines.get(n) == "BLOCKED"]
    still_open = [n for n in range(1, total + 1) if n not in lines]
    return still_open, blocked


def build_continuation(batch: dict, report: Path, still_open: list[int]) -> str:
    """The short user message that names what is still owed."""
    named = "; ".join(
        f"item {n} ({batch['items'][n - 1]['path']})" for n in still_open
    )
    return (f"The batch still has open items: {named}. Continue with them and "
            f"write each one's line in {report}. If one is blocked, say what is "
            "blocking it on its line.")


# ============================================================
# CLI
# ============================================================

def cmd_status(batch: dict | None) -> int:
    if not batch:
        print(f"{GRAY}no batch approved{RESET}")
        return 0
    state = "pending" if is_pending(batch) else f"consumed {batch['consumed_at']}"
    print(f"{BOLD}night batch{RESET}  {len(batch['items'])} item(s), "
          f"{batch['estimated_minutes']}m estimated, approved "
          f"{batch['approved_at']}, {state}")
    for item in batch["items"]:
        print(f"  {CYAN}{item['path']}{RESET}  {item['summary']}")
    return 0


def cmd_approve(max_minutes: int | None) -> int:
    rot = _rotation()
    current = rot.inventory(ROOT)
    entries = rot.load_ledger(rot.data_path("audit-rotation-ledger"))
    findings = rot.open_findings(entries, current)
    if not findings:
        print(f"{GREEN}nothing open to approve{RESET}")
        return 0

    batch = build_batch(findings, max_minutes)
    save_batch(BATCH_PATH, batch)
    print(f"{GREEN}approved{RESET} {len(batch['items'])} item(s), "
          f"{batch['estimated_minutes']}m estimated")
    for item in batch["items"]:
        print(f"  {CYAN}{item['path']}{RESET}  {item['summary']}")
    print(f"\n{GRAY}The night run will not commit or push. The tree is dirty in "
          f"the morning and the decision is yours.{RESET}")
    return 0


def cmd_run(timeout_s: int) -> int:
    held = hold_reason(HOLD_PATH, _now()[:10])
    if held:
        print(f"{YELLOW}HELD{RESET} {held}", file=sys.stderr)
        return 0

    batch = load_batch(BATCH_PATH)
    if not is_pending(batch):
        print(f"{GRAY}no approved batch pending; nothing to do{RESET}")
        return 0

    claude = shutil.which("claude")
    if not claude:
        print(f"{RED}claude CLI not on PATH; the night run cannot start{RESET}",
              file=sys.stderr)
        return 2

    # Consumed BEFORE the session starts. A crash, a reboot or a second timer
    # fire then finds nothing pending, rather than repeating a half-done pass
    # over a tree the first one already changed.
    batch["consumed_at"] = _now()
    save_batch(BATCH_PATH, batch)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    date = _now()[:10]
    log = LOG_DIR / f"{date}-session.log"
    session = str(uuid.uuid4())
    # Named by the session, so a second pass the same day, or a leftover file,
    # cannot hand this one lines it did not write.
    report = LOG_DIR / f"{date}-{session[:8]}.md"
    deadline = time.monotonic() + timeout_s

    print(f"{BOLD}starting the night pass{RESET}  {len(batch['items'])} item(s)")
    # The standing paragraph goes into the system prompt from the first
    # request, where the guidance puts it.
    command = [claude, "-p", "--session-id", session,
               "--append-system-prompt", UNATTENDED_STANDING_INSTRUCTION,
               build_prompt(batch, report)]
    # This script drives its own continuations. The Stop hook's unattended mode
    # must not add a second driver if CLAUDE_HANDOFF_UNATTENDED is set in the
    # environment the night session inherits. Built from os.environ, so the
    # trace id and everything else still pass through.
    child_env = dict(os.environ, CLAUDE_HANDOFF_UNATTENDED="0")
    transcript = []
    continuations = 0
    while True:
        remaining = max(1.0, deadline - time.monotonic())
        try:
            proc = subprocess.run(command, cwd=ROOT, capture_output=True,
                                  text=True, timeout=remaining, env=child_env)
        except subprocess.TimeoutExpired:
            transcript.append(f"TIMED OUT after {timeout_s}s in total\n")
            log.write_text("".join(transcript), encoding="utf-8")
            print(f"{RED}the night session exceeded {timeout_s}s and was stopped"
                  f"{RESET}", file=sys.stderr)
            return 2
        except OSError as exc:
            transcript.append(f"FAILED TO START: {exc}\n")
            log.write_text("".join(transcript), encoding="utf-8")
            print(f"{RED}the night session did not start: {exc}{RESET}",
                  file=sys.stderr)
            return 2
        transcript.append(f"=== session {continuations + 1}: exit "
                          f"{proc.returncode}\n\n{proc.stdout}\n\n"
                          f"--- stderr ---\n{proc.stderr}\n\n")
        log.write_text("".join(transcript), encoding="utf-8")
        still_open, blocked = report_status(batch, report)
        if (proc.returncode != 0 or not still_open
                or continuations >= MAX_CONTINUATIONS
                or time.monotonic() >= deadline):
            break
        continuations += 1
        print(f"{YELLOW}{len(still_open)} item(s) open with no blocker stated; "
              f"continuation {continuations} of {MAX_CONTINUATIONS}{RESET}")
        # The flag again, although the recorded system prompt is normally reused
        # on resume: after a compaction it is rendered afresh, and the later
        # text is ignored whenever the snapshot still holds.
        command = [claude, "-p", "--resume", session,
                   "--append-system-prompt", UNATTENDED_STANDING_INSTRUCTION,
                   build_continuation(batch, report, still_open)]

    total = len(batch["items"])
    colour = GREEN if proc.returncode == 0 and not still_open else YELLOW
    print(f"{colour}night pass ended{RESET} exit {proc.returncode}; "
          f"{total - len(still_open)} of {total} item(s) reported in {report}, "
          f"{continuations} continuation(s); transcript at {log}")
    if blocked:
        print(f"  blocked: {', '.join(f'item {n}' for n in blocked)}")
    if still_open:
        print(f"{YELLOW}  still open, no line in the report: "
              f"{', '.join(f'item {n}' for n in still_open)}{RESET}")
    return 0 if proc.returncode == 0 and not still_open else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--approve", action="store_true",
                    help="approve the open findings as tonight's batch")
    ap.add_argument("--max-minutes", type=int, default=None,
                    help="time budget for the batch")
    ap.add_argument("--run", action="store_true",
                    help="run the approved batch (started by hand; nothing "
                         "schedules this)")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    args = ap.parse_args()

    if args.approve:
        return cmd_approve(args.max_minutes)
    if args.run:
        return cmd_run(args.timeout)
    return cmd_status(load_batch(BATCH_PATH))


if __name__ == "__main__":
    sys.exit(main())
