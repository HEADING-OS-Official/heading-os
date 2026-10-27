#!/usr/bin/env python3
"""Watch the inference host this workspace depends on, and tell the operator when it stops answering.

Every model this workspace uses runs on ONE machine, reached across the WSL NAT
gateway, and nothing falls back: when that server is not answering, embedding
refuses and the chronicle build stops. Autostart covers a reboot; this guard is
what notices a crash between reboots. It probes the addresses
`config/inference-hosts.yaml` names, with the credential each address requires.

**It no longer starts anything.** Until 2026-09-25 its `heal` mode launched the
Windows-side application through WSL interop (`cmd.exe /c start ...`) and
re-probed. The operator decided on 2026-09-25 (HOS-60) that the WSL-only rule
holds with no exception: nothing in this workspace invokes PowerShell, `cmd.exe`
or any other Windows-side tooling. A named exception for this guard was the
alternative, and it was rejected. So the guard probes and REPORTS, and the
server is started where it is served, by the operator.

The report travels by the channel the ops radar already uses for an outage: one
line through `scripts/utils/telegram_notify.py` to the operator's own sink,
resolved from the same variables `scripts/ops-radar-notify.py` reads
(`OPS_RADAR_TELEGRAM_TARGET`, then `ODIN_CADENCE_TELEGRAM_TARGET`). No new
recipient variable and no new transport; `telegram_notify` refuses anything
that is not the operator's own sink, so this is a self-notification in the
sense of `.claude/rules/lethal-trifecta.md` and needs no click. The radar's
daily 08:00 push still reports the same outage (`ollama-accel: pinned embed host
down`), so this guard only shortens the time to hearing about it from up to a
day to about ten minutes.

"Sustained" is the radar's own threshold, `ops_signals.AUTOHEAL_ESCALATE`
consecutive failures, so a single missed probe (the server restarting, a slow
answer) says nothing. One line is sent when an outage becomes sustained and one
when the host answers again after that; nothing in between, however long it
lasts. The wire carries a label and a count and never an address, the same
sovereignty clause the radar's push keeps (`.claude/skills/radar/SKILL.md`,
NEVER). The addresses go to the journal, where "nothing answered at <url> or
<url>" is the next step and "the host is down" would not be.

What it deliberately does NOT do:

* It never restarts, starts, installs or configures an inference server.
* It never sends a line when it cannot remember having sent one. The outage
  state lives in the runtime store under the `inference-guard` service; a tree
  whose registry has no such row refuses the state and the guard reports only
  to its journal, leaving the radar's daily push as the operator's channel.

Usage:
    python scripts/inference-guard.py check          # probe only; exit 0 up, 1 down. Writes nothing, sends nothing.
    python scripts/inference-guard.py watch          # probe, and report a sustained outage to the operator
    python scripts/inference-guard.py check --json

Exit codes: 0 an inference host is answering, 1 nothing answered, 2 nothing to probe
(the host configuration could not be read).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.colors import GRAY, GREEN, RED, RESET
from scripts.utils.ops_signals import AUTOHEAL_ESCALATE, inference_hosts_in_use
from scripts.utils.clone_guard import require_main_clone

# The one file this service keeps in the runtime store, under
# `runtime:inference-guard/state`: the current failure streak and whether the
# operator was told.
STATE_FILENAME = "outage-state.json"

# Consecutive failed probes before an outage counts as sustained. The radar's
# own threshold, so "sustained" means one thing in this workspace.
SUSTAINED_FAILURES = AUTOHEAL_ESCALATE

# The recipient chain `scripts/ops-radar-notify.py` resolves, in its order.
RECIPIENT_ENV_VARS = ("OPS_RADAR_TELEGRAM_TARGET", "ODIN_CADENCE_TELEGRAM_TARGET")


@dataclass
class Outcome:
    """What the probe found, in the words the caller has to report."""

    up: bool
    detail: str


def probe_target(target) -> bool:
    """Reachability of one ARMED target. The seam the tests inject past.

    A free function rather than `inference_host.probe` imported directly, because
    the credential travels with the address: see `inference_host.ProbeTarget`.
    """
    return target.reachable()


def probe_all(targets: list, *, probe=probe_target) -> Outcome:
    """Probe `targets` in order; the first to answer wins.

    Raises:
        ValueError: `targets` is empty. That means the configuration could not
            be read, and reporting "up" from an empty list is a monitor that
            passes by knowing nothing.
    """
    if not targets:
        raise ValueError("no inference address to probe; check config/inference-hosts.yaml")
    for target in targets:
        if probe(target):
            return Outcome(True, target.url)
    return Outcome(False, ", ".join(t.url for t in targets))


def next_state(state: dict, up: bool) -> tuple[dict, str]:
    """Pure: the new outage state, and the line to send ("" for none).

    `notified` flips only when the caller confirms delivery, so this returns the
    state as it should be IF the line is delivered; `watch` keeps the old
    `notified` when the send fails, and the next fire tries again.
    """
    streak = state.get("down_streak", 0)
    streak = streak if isinstance(streak, int) and streak >= 0 else 0
    notified = state.get("notified") is True

    if up:
        line = (
            f"inference host answering again after {streak} failed checks"
            if notified else ""
        )
        return {"down_streak": 0, "notified": False}, line

    streak += 1
    if streak >= SUSTAINED_FAILURES and not notified:
        return (
            {"down_streak": streak, "notified": True},
            f"inference host DOWN: nothing answered for {streak} consecutive checks; "
            f"it is not started from WSL, start it where it is served",
        )
    return {"down_streak": streak, "notified": notified}, ""


def _log(msg: str) -> None:
    print(f"[inference-guard] {msg}", file=sys.stderr)


def _state_path() -> Path:
    """`outage-state.json` in the runtime store `runtime:inference-guard/state`.

    Raises `UnknownService` where the registry lacks the row and
    `DataRootError` where there is no writable overlay; `watch` catches both at
    the call and falls back to the journal alone.
    """
    from scripts.utils.data_path import data_write_dir

    return data_write_dir("runtime:inference-guard/state") / STATE_FILENAME


def _load_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        _log(f"could not read the outage state ({exc}); starting a fresh streak")
        return {}
    return state if isinstance(state, dict) else {}


def _save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, path)
    except OSError as exc:
        _log(f"could not save the outage state ({exc}); the next fire may repeat a line")


def _recipient() -> str:
    return next((os.environ[v] for v in RECIPIENT_ENV_VARS if os.environ.get(v)), "")


def report(up: bool, *, state_path=_state_path, notify=None) -> None:
    """Advance the outage state and send the one line it calls for, if any."""
    from scripts.utils.data_path import DataPathError

    # `DataPathError` is every refusal the state path can raise, the no-overlay
    # one included: the front door re-dresses `DataRootError` as its own. Until
    # 2026-09-25 this named `RuntimeDirError` and `DataRootError`, which is what
    # `get_runtime_dir` raised.
    try:
        path = state_path()
    except DataPathError as exc:
        _log(f"no outage state available ({exc}); journal only, the radar's daily push still reports")
        return

    previous = _load_state(path)
    state, line = next_state(previous, up)
    if line and not _deliver(line, notify):
        # The streak still advances; only `notified` stays as it was, so the
        # next fire computes the same line and tries again.
        state["notified"] = previous.get("notified") is True
    _save_state(path, state)


def _deliver(line: str, notify) -> bool:
    """Send `line` to the operator's own sink. True only on confirmed delivery."""
    recipient = _recipient()
    if not recipient:
        _log(f"no recipient configured ({' / '.join(RECIPIENT_ENV_VARS)}); not sent: {line}")
        return False
    if notify is None:
        from scripts.utils import telegram_notify
        notify = telegram_notify.notify
    if not notify(recipient, line):
        _log(f"not delivered (see telegram_notify log); the next fire retries: {line}")
        return False
    _log(f"reported to the operator: {line}")
    return True


def main() -> int:
    require_main_clone(__file__)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("check", "watch"), nargs="?", default="check")
    parser.add_argument("--json", action="store_true", help="machine-readable result")
    args = parser.parse_args()

    try:
        targets = inference_hosts_in_use()
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"{RED}inference-guard: cannot read the host config: {exc}{RESET}\n")
        return 2
    try:
        outcome = probe_all(targets)
    except ValueError as exc:
        sys.stderr.write(f"{RED}inference-guard: {exc}{RESET}\n")
        return 2

    # A declared-but-unset key variable is reported as itself rather than as an
    # outage: nothing was established about the host, because every probe went
    # out unauthenticated. The message carries the variable NAME and never the
    # key -- that is all `load_api_key` puts in its own text.
    auth_error = next((t.auth_error for t in targets if t.auth_error), "")

    if args.json:
        # URLs only. `ProbeTarget.headers` holds a live bearer token and a
        # `--json` line goes to a log, a journal and a pipe.
        print(json.dumps({
            "up": outcome.up,
            "detail": outcome.detail,
            "hosts": [t.url for t in targets],
            "auth_error": auth_error,
        }, ensure_ascii=False))
    elif outcome.up:
        print(f"{GREEN}inference host up{RESET} {GRAY}{outcome.detail}{RESET}")
    else:
        sys.stderr.write(f"{RED}inference host DOWN: nothing answered at {outcome.detail}{RESET}\n")
        if auth_error:
            sys.stderr.write(
                f"{RED}inference-guard: nothing was established about that host -- "
                f"{auth_error}{RESET}\n"
            )

    if args.command == "watch":
        from scripts.utils.paths import load_env

        # The recipient variables live in the gitignored .env, also under systemd.
        try:
            load_env()
        except (OSError, ValueError) as exc:
            _log(f"could not read .env ({exc}); the recipient may be unset")
        report(outcome.up)

    return 0 if outcome.up else 1


if __name__ == "__main__":
    raise SystemExit(main())
